"""Streamlit HR dashboard — monthly, aggregated, k-anonymous insights."""

from __future__ import annotations

import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import get_settings
from db import get_session_factory, init_db
from logging_utils import setup_logging
from services import hr_insights, hr_metrics, policy_dict, update_policy
from ui_theme import footer, hero, inject_css, insight_cards, kpi_cards, section

setup_logging()
init_db()
SETTINGS = get_settings()

DISCLAIMER = (
    "Wellness guidance only — not medical advice · Aggregated with k-anonymity, no names or employee IDs · "
    "Work-hours energy only, no daily calorie budgets"
)
GREEN = "#0E9F6E"
INSIGHT_TAGS = [
    "Energy trend",
    "Weekly pattern",
    "Departments",
    "Engagement",
    "Peak slump hour",
    "Before-meeting mitigation",
]

st.set_page_config(page_title="Wellness Intelligence · HR", page_icon="📊", layout="wide")
inject_css()


def _with_session(fn, *args, **kwargs):
    session = get_session_factory()()
    try:
        return fn(session, *args, **kwargs)
    finally:
        session.close()


@st.cache_data(ttl=5)
def _load():
    return _with_session(hr_insights, "monthly"), _with_session(hr_metrics)


def _policy_sidebar() -> None:
    policy = _with_session(policy_dict)
    with st.sidebar:
        st.markdown("### ⚙️ Wellness policy")
        st.caption("Applies to the agent immediately · every change is audited")
        with st.form("policy_form", border=False):
            allow_food = st.toggle("Allow food orders", value=policy["allow_food_orders"])
            max_order = st.number_input(
                "Max price per order (SAR)", 0.0, 200.0, float(policy["max_order_sar"]), 1.0
            )
            auto_book = st.toggle("Auto-book walks in calendar", value=policy["auto_book_walk_break"])
            cap = st.number_input(
                "Proactive nudges / employee / day", 0, 10, int(policy["max_proactive_per_day"]), 1
            )
            st.toggle("Employee confirms every spend", value=True, disabled=True, help="Locked on")
            if st.form_submit_button("Save policy", type="primary", use_container_width=True):
                try:
                    _with_session(
                        update_policy,
                        allow_food_orders=allow_food,
                        max_order_sar=max_order,
                        auto_book_walk_break=auto_book,
                        max_proactive_per_day=int(cap),
                    )
                    st.cache_data.clear()
                    st.success("Policy saved")
                except Exception as exc:
                    st.error(f"Could not save: {exc}")
        st.divider()
        st.markdown("### 🔒 Privacy")
        st.caption(
            f"Groups under {SETTINGS.k_anonymity} employees are hidden · meeting titles never leave the "
            "calendar · HR never sees individual meals"
        )
        if st.button("↻ Refresh data", use_container_width=True):
            st.cache_data.clear()
            st.rerun()


_policy_sidebar()
data, metrics = _load()

if not data.get("daily"):
    hero("Wellness Intelligence", "Monthly overview", [("Simulated data", "warn")])
    st.info(data["insights"][0])
    footer(DISCLAIMER)
    st.stop()

k = data["kpis"]
hero(
    "Wellness Intelligence",
    f"Monthly overview · {data['from']} → {data['to']}",
    [
        ("Simulated data", "warn"),
        (f"k-anonymity ≥ {data['k_anonymity']}", ""),
        (f"Opt-in {metrics['opt_in_rate'] * 100:.0f}%", ""),
        (f"{len(data['departments'])} departments", ""),
    ],
)

delta = k["energy_last_week"] - k["energy_first_week"]
kpi_cards(
    [
        {
            "label": "Team energy index",
            "value": f"{k['energy_last_week']:.1f}",
            "sub": f"{'▲' if delta >= 0 else '▼'} {abs(delta):.1f} vs first week",
            "trend": "up" if delta >= 0 else "down",
            "icon": "⚡",
        },
        {
            "label": "Acceptance rate",
            "value": f"{k['acceptance_rate']:.0%}",
            "sub": f"{k['accepted']:,} of {k['offered']:,} suggestions",
            "icon": "🤝",
        },
        {
            "label": "Slumps prevented",
            "value": f"{k['slumps_prevented']:,}",
            "sub": "verified walks, snacks, hydration",
            "icon": "🛡️",
        },
        {
            "label": "Peak slump hour",
            "value": k["peak_slump_hour"],
            "sub": f"{k['peak_slump_label'] or 'No data'} · {k['peak_slump_share']:.0%} of risk flags",
            "icon": "🕑",
        },
        {
            "label": "Mitigated before meetings",
            "value": f"{k['mitigated_before_meetings']:,}",
            "sub": f"{k['mitigation_rate']:.0%} of {k['meeting_linked']:,} meeting-linked nudges",
            "trend": "up",
            "icon": "📅",
        },
    ]
)

section("Monthly insights", "What the agent learned across the organisation this month")
insight_cards(list(zip(INSIGHT_TAGS, data["insights"], strict=False)))

daily = pd.DataFrame(data["daily"])
daily["date"] = pd.to_datetime(daily["date"])

left, right = st.columns([1.6, 1])
with left, st.container(border=True):
    section("Team energy index", "Daily average across visible departments")
    base = alt.Chart(daily).encode(x=alt.X("date:T", title=None, axis=alt.Axis(format="%d %b")))
    lo, hi = daily["energy_index"].min(), daily["energy_index"].max()
    area = base.mark_area(
        clip=True,
        line={"color": GREEN, "strokeWidth": 2.5},
        color=alt.Gradient(
            gradient="linear",
            stops=[alt.GradientStop(color="#A7F3D0", offset=0), alt.GradientStop(color="#ffffff", offset=1)],
            x1=1, x2=1, y1=0, y2=1,
        ),
    ).encode(
        y=alt.Y("energy_index:Q", title=None, scale=alt.Scale(domain=[lo - 4, hi + 2])),
        tooltip=[alt.Tooltip("date:T", format="%d %b"), alt.Tooltip("energy_index:Q", title="Energy")],
    )
    st.altair_chart(area.properties(height=260), use_container_width=True)

with right, st.container(border=True):
    section("Energy by weekday", "Lowest day highlighted")
    weekday = pd.DataFrame(data["weekday"])
    low = weekday["avg_energy"].min()
    weekday["is_low"] = weekday["avg_energy"] == low
    bars = alt.Chart(weekday).mark_bar(cornerRadiusTopLeft=6, cornerRadiusTopRight=6).encode(
        x=alt.X("weekday:N", sort=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], title=None),
        y=alt.Y("avg_energy:Q", title=None, scale=alt.Scale(zero=False)),
        color=alt.condition("datum.is_low", alt.value("#F59E0B"), alt.value(GREEN)),
        tooltip=["weekday", alt.Tooltip("avg_energy:Q", title="Energy")],
    )
    st.altair_chart(bars.properties(height=260), use_container_width=True)

left, right = st.columns([1.6, 1])
with left, st.container(border=True):
    section("Energy by department", "7-day rolling average · last 30 days")
    series = pd.DataFrame(metrics.get("series") or [])
    if not series.empty:
        series["date"] = pd.to_datetime(series["date"])
        series = (
            series.groupby(["department", "date"], as_index=False)["energy_index"].mean()
            .sort_values("date")
        )
        series["energy_7d"] = series.groupby("department")["energy_index"].transform(
            lambda s: s.rolling(7, min_periods=1).mean()
        )
        series = series[series["date"] >= daily["date"].min()]
        lines = alt.Chart(series).mark_line(strokeWidth=2.5, interpolate="monotone").encode(
            x=alt.X("date:T", title=None, axis=alt.Axis(format="%d %b")),
            y=alt.Y("energy_7d:Q", title=None, scale=alt.Scale(zero=False)),
            color=alt.Color("department:N", legend=alt.Legend(orient="bottom", title=None),
                            scale=alt.Scale(scheme="tableau10")),
            tooltip=["department", alt.Tooltip("date:T", format="%d %b"),
                     alt.Tooltip("energy_7d:Q", title="Energy (7d)", format=".1f")],
        )
        st.altair_chart(lines.properties(height=280), use_container_width=True)

with right, st.container(border=True):
    section("Engagement", "Suggestions offered vs accepted per day")
    eng = daily.melt(id_vars="date", value_vars=["offered", "accepted"], var_name="kind", value_name="count")
    eng_chart = alt.Chart(eng).mark_area(opacity=0.55, interpolate="monotone").encode(
        x=alt.X("date:T", title=None, axis=alt.Axis(format="%d %b")),
        y=alt.Y("count:Q", title=None, stack=None),
        color=alt.Color("kind:N", scale=alt.Scale(domain=["offered", "accepted"], range=["#CBD5E1", GREEN]),
                        legend=alt.Legend(orient="bottom", title=None)),
    )
    st.altair_chart(eng_chart.properties(height=280), use_container_width=True)

with st.container(border=True):
    section("Department scorecard", "Sorted by average energy · groups below k are hidden")
    dept = pd.DataFrame(data["departments"]).sort_values("avg_energy", ascending=False)
    dept["acceptance_rate"] = dept["acceptance_rate"] * 100
    st.dataframe(
        dept[
            [
                "department",
                "avg_energy",
                "acceptance_rate",
                "slumps_prevented",
                "mitigated_before_meetings",
                "offered",
            ]
        ],
        hide_index=True,
        use_container_width=True,
        column_config={
            "department": st.column_config.TextColumn("Department"),
            "avg_energy": st.column_config.ProgressColumn(
                "Avg energy", min_value=0, max_value=100, format="%.1f"
            ),
            "acceptance_rate": st.column_config.ProgressColumn(
                "Acceptance", min_value=0, max_value=100, format="%.0f%%"
            ),
            "slumps_prevented": st.column_config.NumberColumn("Slumps prevented"),
            "offered": st.column_config.NumberColumn("Suggestions"),
            "mitigated_before_meetings": st.column_config.NumberColumn("Mitigated before meetings"),
        },
    )

footer(DISCLAIMER)
