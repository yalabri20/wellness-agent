"""Streamlit chat simulator — Channel + calendar + walk closed-loop for the demo."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import streamlit as st

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_core import Orchestrator
from calendar_provider import MockCalendar
from channels import is_placeholder_url, pull_outbox
from config import DEMO_IMAGES_DIR, get_settings
from db import get_session_factory, init_db, now
from demo_mode import effective_agent_now, ensure_demo_response_files
from interventions import (
    accept_intervention,
    apply_activity,
    complete_intervention,
    get_activity_state,
    get_calendar_block,
    get_intervention,
    list_interventions,
    verify_open_interventions,
)
from logging_utils import setup_logging
from models import ChannelMessage
from scheduler import SIMULATOR_CHANNEL, scan_and_notify
from services import (
    add_calendar_event,
    confirm_and_create_order,
    delete_calendar_event,
    get_order,
    get_wallet,
    list_calendar_events,
    list_meal_logs,
    mark_decision_skipped,
)
from ui_theme import esc, footer, hero, inject_css, section

setup_logging()
ensure_demo_response_files()
init_db()

DISCLAIMER = (
    "Wellness guidance only — not medical advice · "
    "Work-hours energy only, no daily calorie budgets"
)
SETTINGS = get_settings()
DEMO_USER = SETTINGS.live_demo_user_id
API = SETTINGS.api_base_url
LAN_IP = "192.168.8.108"

st.set_page_config(page_title="Wellness Agent", page_icon="🌿", layout="wide")
inject_css()

for key, default in [
    ("thread", []),
    ("order_id", None),
    ("last_decision", None),
    ("pending_meal_id", None),
    ("pending_correlation", None),
    ("pending_intervention_id", None),
    ("tracking_walk_id", None),
    ("walk_was_pending", False),
    ("why_text", None),
]:
    if key not in st.session_state:
        st.session_state[key] = default


def _append(role: str, text: str, message: ChannelMessage | None = None) -> None:
    st.session_state.thread.append(
        {"role": role, "text": text, "message": message.model_dump() if message else None}
    )


def _run_meal(text: str | None, image_bytes: bytes | None, image_key_hint: str | None = None) -> None:
    session = get_session_factory()()
    try:
        orch = Orchestrator(session)
        decision, msg = orch.handle_meal(
            DEMO_USER,
            text=text,
            image_bytes=image_bytes,
            image_key_hint=image_key_hint,
            thread_id="sim-thread",
        )
        st.session_state.last_decision = decision.model_dump(mode="json")
        st.session_state.pending_correlation = decision.correlation_id
        if decision.intervention and decision.intervention.meal_id:
            st.session_state.pending_meal_id = decision.intervention.meal_id
        st.session_state.pending_intervention_id = msg.metadata.get("intervention_id")
        st.session_state.why_text = (
            decision.assessment.reasoning if decision.assessment else msg.metadata.get("why")
        )
        _append("agent", msg.text, msg)
    finally:
        session.close()


def _confirm() -> None:
    meal_id = st.session_state.pending_meal_id
    if not meal_id:
        st.warning("No snack pending confirmation.")
        return
    session = get_session_factory()()
    try:
        token = f"sim-{st.session_state.pending_correlation or 'local'}-{meal_id}"
        order = confirm_and_create_order(
            session,
            user_id=DEMO_USER,
            meal_id=meal_id,
            confirmation_token=token,
            correlation_id=st.session_state.pending_correlation,
            confirmed=True,
        )
        st.session_state.order_id = order.id
        _append(
            "agent",
            f"✅ Order confirmed. Status: **{order.status.value}** · "
            f"{order.price_sar:.0f} SAR from your wellness allowance. ETA ~{order.eta_minutes} min.",
        )
    except Exception as exc:
        _append("agent", f"Could not confirm order: {exc}")
    finally:
        session.close()


def _start_walk(intervention_id: str) -> None:
    session = get_session_factory()()
    try:
        rec = accept_intervention(session, intervention_id, start_progress=True)
        block = get_calendar_block(session, rec)
        lines = []
        if block is not None:
            lines.append(
                f"📅 Booked **{block.start.strftime('%H:%M')}–{block.end.strftime('%H:%M')}** "
                "in your calendar as a wellness walk (ends before your next meeting)."
            )
        if rec.baseline_pending:
            lines.append(
                f"🚶 Walk **IN_PROGRESS** · target **+{rec.target_steps or 0} steps**.\n\n"
                "📱 Run your **phone Shortcut now** (or update steps manually) to lock your "
                "starting steps — then walk and sync again."
            )
        else:
            start = rec.baseline_steps or 0
            goal = start + (rec.target_steps or 0)
            lines.append(
                f"🚶 Walk **IN_PROGRESS** · steps **{start}** → goal **{goal}** "
                f"(+{rec.target_steps or 0}).\n\n"
                f"Walk, then sync from your phone Shortcut. At **{goal}+** the agent marks **COMPLETED**."
            )
        _append("agent", "\n\n".join(lines))
        st.session_state.pending_intervention_id = intervention_id
        st.session_state.tracking_walk_id = intervention_id
        st.session_state.walk_was_pending = rec.baseline_pending
    except Exception as exc:
        _append("agent", f"Could not start walk: {exc}")
    finally:
        session.close()


def _mark_done(intervention_id: str) -> None:
    session = get_session_factory()()
    try:
        complete_intervention(session, intervention_id, reason="employee_done")
        _append("agent", "✅ Nice — marked as done. Logged for your team's energy trend.")
    except Exception as exc:
        _append("agent", f"Could not mark done: {exc}")
    finally:
        session.close()


def _skip() -> None:
    session = get_session_factory()()
    try:
        if st.session_state.pending_intervention_id:
            from interventions import decline_intervention

            decline_intervention(session, st.session_state.pending_intervention_id)
        else:
            mark_decision_skipped(session, DEMO_USER, st.session_state.pending_correlation)
    finally:
        session.close()
    st.session_state.tracking_walk_id = None
    _append("agent", "Skipped — no allowance / walk required.")


def _sync_background_events() -> None:
    """Pull scheduler messages, expire stale suggestions, announce phone-verified walks."""
    for msg in pull_outbox(DEMO_USER):
        if msg.metadata.get("meal_id"):
            st.session_state.pending_meal_id = msg.metadata["meal_id"]
        st.session_state.pending_correlation = msg.metadata.get("correlation_id")
        st.session_state.pending_intervention_id = msg.metadata.get("intervention_id")
        st.session_state.why_text = msg.metadata.get("why")
        _append("agent", msg.text, msg)

    session = get_session_factory()()
    try:
        verify_open_interventions(session, now(session))
        walk_id = st.session_state.tracking_walk_id
        if not walk_id:
            return
        try:
            rec = get_intervention(session, walk_id)
        except Exception:
            st.session_state.tracking_walk_id = None
            return
        goal = (rec.baseline_steps or 0) + (rec.target_steps or 0)
        if st.session_state.get("walk_was_pending") and not rec.baseline_pending:
            st.session_state.walk_was_pending = False
            if rec.status.value == "in_progress":
                _append(
                    "agent",
                    f"📱 Start locked at **{rec.baseline_steps}** steps → goal **{goal}**. "
                    "Go walk, then sync again.",
                )
        if rec.status.value == "completed":
            _append(
                "agent",
                f"🎉 Walk verified — **{rec.baseline_steps + rec.current_steps}/{goal}** steps. "
                "Intervention **COMPLETED**. You're set for your meeting.",
            )
            st.session_state.tracking_walk_id = None
        elif rec.status.value in ("expired", "declined"):
            _append("agent", f"Walk {rec.status.value} — the meeting window has passed.")
            st.session_state.tracking_walk_id = None
    finally:
        session.close()


def _snapshot() -> dict:
    """Everything the header and side cards need, in one DB session."""
    session = get_session_factory()()
    try:
        clock = effective_agent_now(session)
        state = get_activity_state(session, DEMO_USER)
        upcoming = [e for e in MockCalendar(session).get_events(DEMO_USER, clock) if e.start > clock]
        try:
            wallet = get_wallet(session, DEMO_USER)
        except Exception:
            wallet = None
        order = None
        if st.session_state.order_id:
            try:
                order = get_order(session, st.session_state.order_id)
            except Exception:
                order = None
        walks = [
            r
            for r in list_interventions(session, limit=20)
            if r.user_id == DEMO_USER and r.type.value in ("walk", "movement_break")
        ]
        return {
            "clock": clock,
            "steps": state.steps,
            "steps_updated": state.updated_at,
            "steps_source": state.source,
            "next_meeting": upcoming[0] if upcoming else None,
            "wallet": wallet,
            "order": order,
            "meals": list_meal_logs(session, DEMO_USER, day=clock),
            "events": list_calendar_events(session, DEMO_USER),
            "walks": walks,
        }
    finally:
        session.close()


_sync_background_events()
if st.session_state.tracking_walk_id:
    # Poll while a walk is running so a phone sync shows up without clicking
    try:
        from streamlit_autorefresh import st_autorefresh  # type: ignore

        st_autorefresh(interval=4000, key="walk_refresh")
    except Exception:
        pass

snap = _snapshot()
nm = snap["next_meeting"]
next_pill = (
    f"📅 Next: {nm.start.strftime('%H:%M')} · {nm.importance.value}"
    if nm
    else "📅 No more meetings today"
)
wallet_pill = (
    f"💳 {snap['wallet'].daily_remaining_sar:.0f} / {snap['wallet'].daily_allowance_sar:.0f} SAR today"
    if snap["wallet"]
    else "💳 —"
)
hero(
    "Wellness Agent",
    "Your work-hours energy coach — reads your calendar, meals and steps, then suggests one action.",
    [
        (f"🕒 {snap['clock'].strftime('%H:%M')}", ""),
        (next_pill, ""),
        (f"🚶 {snap['steps']:,} steps", ""),
        (wallet_pill, ""),
        ("🚶 Walk in progress" if st.session_state.tracking_walk_id else "● Agent online", "ok"),
    ],
)

tab_chat, tab_cal, tab_walk = st.tabs(["💬  Coach", "📅  Calendar", "🚶  Activity"])

# ---------------------------------------------------------------- Coach
with tab_chat:
    col_main, col_side = st.columns([2.1, 1], gap="large")
    with col_main:
        if not st.session_state.thread:
            with st.chat_message("assistant", avatar="🌿"):
                st.markdown(
                    "**Hi! I'm your wellness agent.** Tell me what you ate (Arabic or English) or "
                    "send a photo — I'll check your next meeting and suggest one quick action."
                )
        for item in st.session_state.thread:
            role = "user" if item["role"] == "user" else "assistant"
            avatar = "🧑‍💼" if role == "user" else "🌿"
            with st.chat_message(role, avatar=avatar):
                st.markdown(item["text"])

        last = next((t for t in reversed(st.session_state.thread) if t["role"] == "agent"), None)
        if last and last.get("message"):
            buttons = last["message"].get("buttons") or []
            if buttons:
                bcols = st.columns(min(5, len(buttons)))
                for i, btn in enumerate(buttons):
                    with bcols[i % len(bcols)]:
                        action = btn.get("action_id")
                        label = btn.get("label", action)
                        primary = action in ("confirm_snack", "start_walk", "mark_done")
                        if action == "order_external" and btn.get("url") and not is_placeholder_url(btn["url"]):
                            st.link_button(label, btn["url"], use_container_width=True)
                        elif action == "order_external_unavailable":
                            st.button(label, disabled=True, key=f"dis_{i}", use_container_width=True)
                        elif st.button(
                            label,
                            key=f"btn_{action}_{i}",
                            type="primary" if primary else "secondary",
                            use_container_width=True,
                        ):
                            if action == "confirm_snack":
                                _confirm()
                            elif action == "start_walk":
                                _start_walk(btn.get("value") or st.session_state.pending_intervention_id)
                            elif action == "mark_done":
                                _mark_done(btn.get("value") or st.session_state.pending_intervention_id)
                            elif action == "skip":
                                _skip()
                            elif action == "remind_later":
                                _append("agent", "I'll remind you later (work hours / caps).")
                            elif action == "why":
                                why = st.session_state.why_text or btn.get("value") or "See above."
                                _append("agent", f"**Why this suggestion?**\n\n{why}")
                            st.rerun()

        with st.container(border=True):
            section("🍽️ Log a meal", "Type it, upload a photo, or pick a demo image")
            meal_text = st.text_input(
                "Meal",
                placeholder="e.g. كبسة دجاج مع بيبسي · grilled chicken salad",
                label_visibility="collapsed",
            )
            u1, u2 = st.columns([1.4, 1])
            with u1:
                upload = st.file_uploader("Photo", type=["jpg", "jpeg", "png"], label_visibility="collapsed")
            with u2:
                demo_img = st.selectbox(
                    "Demo image",
                    ["(none)", "heavy.jpg", "balanced.jpg", "light.jpg"],
                    label_visibility="collapsed",
                )
            if st.button("Send to agent  ➜", type="primary", use_container_width=True):
                image_bytes = None
                hint = None
                display = meal_text
                if upload is not None:
                    image_bytes = upload.read()
                    display = display or f"📷 {upload.name}"
                elif demo_img != "(none)":
                    image_bytes = (DEMO_IMAGES_DIR / demo_img).read_bytes()
                    hint = demo_img.split(".")[0]
                    display = display or f"📷 demo image {demo_img}"
                if not display and not image_bytes:
                    st.warning("Enter text or choose an image.")
                else:
                    _append("user", display or "📷 image")
                    with st.spinner("Checking your calendar, meal and energy…"):
                        _run_meal(meal_text or None, image_bytes, image_key_hint=hint)
                    st.rerun()

    with col_side:
        with st.container(border=True):
            section("🍽️ Meals logged today", "Private to you — HR only sees anonymous totals")
            if snap["meals"]:
                rows = "".join(
                    f'<div class="meal"><div><div class="dish">{esc(m["dishes"])}</div>'
                    f'<div class="meta">{esc(m["time"])} · {esc(m["source"])}</div></div>'
                    f'<div class="meta">~{m["kcal"] or 0:.0f} kcal · {m["protein_g"] or 0:.0f}g protein</div></div>'
                    for m in reversed(snap["meals"])
                )
                st.markdown(rows, unsafe_allow_html=True)
            else:
                st.caption("No meals yet today.")

        with st.container(border=True):
            section("🥗 Snack order")
            order = snap["order"]
            if order:
                flow = ["pending", "preparing", "delivered"]
                idx = flow.index(order.status.value) if order.status.value in flow else 0
                steps_html = "".join(
                    f'<div class="s {"on" if i <= idx else ""}">{s.title()}</div>' for i, s in enumerate(flow)
                )
                st.markdown(
                    f'<div class="meta">{order.price_sar:.0f} SAR · ETA {order.eta_minutes or 0} min</div>'
                    f'<div class="steps">{steps_html}</div>',
                    unsafe_allow_html=True,
                )
            else:
                st.caption("No active order. Snacks are paid from your wellness allowance, only after you confirm.")

        with st.container(border=True):
            section("🎬 Demo controls")
            if st.button("Simulate 12:20 proactive nudge", type="primary", use_container_width=True):
                result = scan_and_notify(SIMULATOR_CHANNEL, only_user_id=DEMO_USER, force_demo_1220=True)
                for _mid, uid, msg in reversed(SIMULATOR_CHANNEL.messages):
                    if uid == DEMO_USER:
                        if msg.metadata.get("meal_id"):
                            st.session_state.pending_meal_id = msg.metadata["meal_id"]
                        st.session_state.pending_correlation = msg.metadata.get("correlation_id")
                        st.session_state.pending_intervention_id = msg.metadata.get("intervention_id")
                        st.session_state.why_text = msg.metadata.get("why")
                        _append("agent", msg.text, msg)
                        break
                st.toast(f"Proactive: {result.messages_sent} message(s)")
                st.rerun()
            if st.button("Reset demo data", use_container_width=True):
                try:
                    httpx.post(f"{API}/demo/reset", timeout=30)
                except Exception:
                    from db import reset_and_seed

                    reset_and_seed()
                st.session_state.thread = []
                st.session_state.order_id = None
                st.session_state.pending_intervention_id = None
                st.session_state.tracking_walk_id = None
                st.session_state.walk_was_pending = False
                st.toast("Demo data reset")
                st.rerun()
            st.caption("Outside work hours the demo clock snaps to 12:20 so today's meetings stay visible.")

# ---------------------------------------------------------------- Calendar
with tab_cal:
    left, right = st.columns([1.5, 1], gap="large")
    with left, st.container(border=True):
        section(
            f"Today · {snap['clock'].strftime('%A %d %b')}",
            "The agent reads only time, importance and attendee count — labels stay here",
        )
        if not snap["events"]:
            st.info("No events — add one on the right.")
        for ev in snap["events"]:
            start = datetime.fromisoformat(ev["start"])
            end = datetime.fromisoformat(ev["end"])
            kind = "wellness" if ev.get("is_wellness_block") else ev["importance"]
            past = " past" if end <= snap["clock"] else ""
            chip = "wellness walk" if kind == "wellness" else ev["importance"]
            detail = (
                "Auto-booked by the agent · not a meeting"
                if kind == "wellness"
                else f"{ev['attendee_count']} attendees"
            )
            c_ev, c_del = st.columns([12, 1])
            with c_ev:
                st.markdown(
                    f'<div class="ev {kind}{past}"><div class="time">{start:%H:%M}–{end:%H:%M}</div>'
                    f'<div class="what"><b>{esc(ev.get("label") or "Meeting")}</b><br>'
                    f'<span class="meta">{esc(detail)}</span></div>'
                    f'<span class="chip {kind}">{esc(chip)}</span></div>',
                    unsafe_allow_html=True,
                )
            with c_del:
                if st.button("✕", key=f"del_{ev['id']}", help="Delete"):
                    session = get_session_factory()()
                    try:
                        delete_calendar_event(session, DEMO_USER, ev["id"])
                    finally:
                        session.close()
                    st.rerun()

    with right, st.container(border=True):
        section("➕ Add meeting")
        c1, c2 = st.columns(2)
        with c1:
            start_t = st.time_input("Start", value=datetime.now().replace(hour=11, minute=0).time())
        with c2:
            end_t = st.time_input("End", value=datetime.now().replace(hour=11, minute=45).time())
        importance = st.radio("Importance", ["low", "medium", "high"], index=2, horizontal=True)
        label = st.text_input("Label (only you see it)", value="Team sync")
        attendees = st.number_input("Attendees", min_value=1, max_value=50, value=4)
        if st.button("Add to calendar", type="primary", use_container_width=True):
            day = snap["clock"].date()
            start_dt = datetime.combine(day, start_t)
            end_dt = datetime.combine(day, end_t)
            if end_dt <= start_dt:
                end_dt = start_dt + timedelta(minutes=30)
            session = get_session_factory()()
            try:
                add_calendar_event(
                    session,
                    DEMO_USER,
                    start=start_dt,
                    end=end_dt,
                    importance=importance,
                    attendee_count=int(attendees),
                    label=label or None,
                )
            finally:
                session.close()
            st.toast("Meeting added — log a meal to see the agent use it")
            st.rerun()

# ---------------------------------------------------------------- Activity
with tab_walk:
    left, right = st.columns([1.3, 1], gap="large")
    with left:
        with st.container(border=True):
            section("🚶 Steps today", f"Last sync {snap['steps_updated']:%H:%M} · source: {snap['steps_source']}")
            st.markdown(
                f'<div class="kpi" style="border:none;padding:0"><div class="value" style="font-size:2.6rem">'
                f'{snap["steps"]:,}</div></div>',
                unsafe_allow_html=True,
            )
            active = next(
                (r for r in snap["walks"] if r.status.value in ("accepted", "in_progress")), None
            )
            if active:
                if active.baseline_pending:
                    st.info("Walk started — run your phone Shortcut once to lock your starting steps.")
                else:
                    target = active.target_steps or 1
                    done = min(active.current_steps, target)
                    st.progress(
                        done / target,
                        text=f"Current walk: +{active.current_steps:,} / +{target:,} steps",
                    )
            else:
                st.caption("No walk in progress. Start one from the Coach tab.")

        with st.container(border=True):
            section("✍️ Update steps manually", "Use this if the phone isn't available")
            m1, m2 = st.columns([2, 1])
            with m1:
                new_total = st.number_input(
                    "Total steps today",
                    min_value=0,
                    max_value=100_000,
                    value=int(snap["steps"]),
                    step=100,
                    label_visibility="collapsed",
                )
            with m2:
                if st.button("Update", type="primary", use_container_width=True):
                    session = get_session_factory()()
                    try:
                        result = apply_activity(session, user_id=DEMO_USER, absolute_steps=int(new_total), source="manual")
                        st.toast(result.get("message") or "Updated")
                    finally:
                        session.close()
                    st.rerun()

        if snap["walks"]:
            with st.container(border=True):
                section("History", "Your recent walks and movement breaks")
                icons = {"proposed": "⏳", "accepted": "👍", "in_progress": "🚶", "completed": "✅",
                         "declined": "⏭", "expired": "⌛"}
                for r in snap["walks"][:6]:
                    progress = (
                        "waiting for first sync"
                        if r.baseline_pending
                        else f"+{r.current_steps:,} / +{r.target_steps or 0:,} steps"
                    )
                    st.markdown(
                        f"{icons.get(r.status.value, '•')} **{r.type.value.replace('_', ' ').title()}** · "
                        f"`{r.status.value}` · {progress} · {r.created_at:%H:%M}"
                    )

    with right, st.container(border=True):
        section("📱 iPhone Shortcut", "Sends your Apple Health steps to the agent")
        st.markdown(
            "1. **Find Health Samples** → Steps, today\n"
            "2. **Get Details** → Value\n"
            "3. **Get Contents of URL** → POST, JSON body below\n"
            "4. Run it before and after your walk"
        )
        st.code(f"POST http://{LAN_IP}:8000/activity/steps", language="text")
        st.code('{\n  "steps": 3842\n}', language="json")
        st.caption("Phone and PC must be on the same Wi-Fi.")

footer(DISCLAIMER)
