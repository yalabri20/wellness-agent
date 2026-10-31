"""Slack Bolt Socket Mode channel — agent core has no Slack imports."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from agent_core import Orchestrator
from channels import ChannelMessage, is_placeholder_url
from config import get_settings
from db import get_session_factory, init_db
from logging_utils import setup_logging
from services import confirm_and_create_order, mark_decision_skipped

logger = logging.getLogger(__name__)
settings = get_settings()

MAX_IMAGE_BYTES = 5 * 1024 * 1024
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/jpg", "image/webp"}


def _blocks_from_message(message: ChannelMessage) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": message.text[:2900]}},
    ]
    actions: list[dict[str, Any]] = []
    for btn in message.buttons:
        if btn.url and not is_placeholder_url(btn.url):
            actions.append(
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": btn.label[:75]},
                    "action_id": btn.action_id,
                    "url": btn.url,
                }
            )
        elif btn.action_id == "order_external_unavailable":
            continue
        else:
            el: dict[str, Any] = {
                "type": "button",
                "text": {"type": "plain_text", "text": btn.label[:75]},
                "action_id": btn.action_id,
                "value": btn.value or btn.action_id,
            }
            if btn.style == "primary":
                el["style"] = "primary"
            if btn.style == "danger":
                el["style"] = "danger"
            actions.append(el)
    if actions:
        blocks.append({"type": "actions", "elements": actions[:5]})
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Wellness guidance only — not medical advice. · Simulated data on HR dashboard.",
                }
            ],
        }
    )
    return blocks


class SlackChannel:
    """Channel implementation using Slack Web API."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self._user_channels: dict[str, str] = {}

    def _resolve_dm(self, user_id: str) -> str:
        if user_id in self._user_channels:
            return self._user_channels[user_id]
        resp = self.client.conversations_open(users=user_id)
        channel = resp["channel"]["id"]
        self._user_channels[user_id] = channel
        return channel

    def send_reply(self, user_id: str, message: ChannelMessage) -> str:
        channel = message.thread_id.split(":")[0] if message.thread_id and ":" in (
            message.thread_id or ""
        ) else self._resolve_dm(user_id)
        thread_ts = None
        if message.thread_id and ":" in message.thread_id:
            channel, thread_ts = message.thread_id.split(":", 1)
        elif message.thread_id:
            thread_ts = message.thread_id
        resp = self.client.chat_postMessage(
            channel=channel,
            text=message.text[:3000],
            blocks=_blocks_from_message(message),
            thread_ts=thread_ts,
        )
        return resp["ts"]

    def send_proactive(self, user_id: str, message: ChannelMessage) -> str:
        # Map demo_user -> configured Slack user id when present
        slack_uid = user_id
        if user_id == settings.live_demo_user_id and settings.live_demo_slack_user_id:
            slack_uid = settings.live_demo_slack_user_id
        return self.send_reply(slack_uid, message)

    def update_message(self, message_id: str, message: ChannelMessage) -> None:
        # message_id format channel:ts
        if ":" not in message_id:
            return
        channel, ts = message_id.split(":", 1)
        self.client.chat_update(
            channel=channel,
            ts=ts,
            text=message.text[:3000],
            blocks=_blocks_from_message(message),
        )


def _download_file(url: str, token: str) -> tuple[bytes, str]:
    with httpx.Client(timeout=20.0) as client:
        resp = client.get(url, headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        return resp.content, ctype


def _map_slack_user(slack_user_id: str) -> str:
    if (
        settings.live_demo_slack_user_id
        and slack_user_id == settings.live_demo_slack_user_id
    ):
        return settings.live_demo_user_id
    return settings.live_demo_user_id  # demo: all traffic maps to demo_user


def build_app() -> App:
    """Construct the Slack Bolt app with handlers."""
    if not settings.slack_bot_token:
        raise RuntimeError("SLACK_BOT_TOKEN missing")
    app = App(
        token=settings.slack_bot_token,
        signing_secret=settings.slack_signing_secret or None,
    )
    channel_api = SlackChannel(app.client)

    def _process_meal(
        *,
        slack_user: str,
        channel_id: str,
        thread_ts: str,
        text: str | None,
        image_bytes: bytes | None = None,
        image_mime: str = "image/jpeg",
    ) -> None:
        user_id = _map_slack_user(slack_user)
        session = get_session_factory()()
        try:
            orch = Orchestrator(session)
            _, msg = orch.handle_meal(
                user_id,
                text=text,
                image_bytes=image_bytes,
                image_mime=image_mime,
                thread_id=f"{channel_id}:{thread_ts}",
            )
            channel_api.send_reply(slack_user, msg)
        finally:
            session.close()

    @app.event("message")
    def handle_message(event: dict, say, ack, client):  # noqa: ANN001
        ack()
        if event.get("subtype") or event.get("bot_id"):
            return
        text = (event.get("text") or "").strip()
        user = event.get("user")
        channel_id = event["channel"]
        ts = event.get("thread_ts") or event["ts"]
        files = event.get("files") or []
        image_bytes = None
        mime = "image/jpeg"
        if files:
            f0 = files[0]
            ftype = f0.get("mimetype", "")
            if ftype not in ALLOWED_TYPES:
                say(text="Please upload a JPEG/PNG image under 5MB.", thread_ts=ts)
                return
            url = f0.get("url_private_download") or f0.get("url_private")
            if url:
                data, mime = _download_file(url, settings.slack_bot_token)
                if len(data) > MAX_IMAGE_BYTES:
                    say(text="Image too large (max 5MB).", thread_ts=ts)
                    return
                image_bytes = data
        if not text and not image_bytes:
            return
        # Strip bot mention
        text_clean = re.sub(r"<@[^>]+>", "", text).strip() or None
        _process_meal(
            slack_user=user,
            channel_id=channel_id,
            thread_ts=ts,
            text=text_clean,
            image_bytes=image_bytes,
            image_mime=mime,
        )

    @app.event("app_mention")
    def handle_mention(event: dict, ack):  # noqa: ANN001
        ack()
        text = re.sub(r"<@[^>]+>", "", event.get("text") or "").strip()
        ts = event.get("thread_ts") or event["ts"]
        _process_meal(
            slack_user=event["user"],
            channel_id=event["channel"],
            thread_ts=ts,
            text=text or None,
        )

    @app.event("file_shared")
    def handle_file_shared(event: dict, ack, client):  # noqa: ANN001
        ack()
        # Prefer message events with files; this is a backup no-op ack
        logger.info("file_shared event ack: %s", event.get("file_id"))

    @app.action("confirm_snack")
    def on_confirm(ack, body, client):  # noqa: ANN001
        ack()
        user = body["user"]["id"]
        meal_id = body["actions"][0].get("value")
        channel_id = body["channel"]["id"]
        thread_ts = body.get("message", {}).get("thread_ts") or body["message"]["ts"]
        token = f"slack-{body['message']['ts']}-{meal_id}"
        session = get_session_factory()()
        try:
            order = confirm_and_create_order(
                session,
                user_id=_map_slack_user(user),
                meal_id=meal_id,
                confirmation_token=token,
                confirmed=True,
            )
            client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=(
                    f"✅ Order confirmed ({order.status.value}). "
                    f"{order.price_sar:.0f} SAR deducted from wellness allowance."
                ),
            )
        except Exception as exc:
            client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=f"Could not confirm: {exc}",
            )
        finally:
            session.close()

    @app.action("skip")
    def on_skip(ack, body, client):  # noqa: ANN001
        ack()
        session = get_session_factory()()
        try:
            mark_decision_skipped(session, _map_slack_user(body["user"]["id"]))
        finally:
            session.close()
        client.chat_postMessage(
            channel=body["channel"]["id"],
            thread_ts=body.get("message", {}).get("thread_ts") or body["message"]["ts"],
            text="Skipped — no allowance used.",
        )

    def _thread_reply(client, body, text: str) -> None:  # noqa: ANN001
        client.chat_postMessage(
            channel=body["channel"]["id"],
            thread_ts=body.get("message", {}).get("thread_ts") or body["message"]["ts"],
            text=text,
        )

    @app.action("start_walk")
    def on_start_walk(ack, body, client):  # noqa: ANN001
        ack()
        from interventions import accept_intervention, get_calendar_block

        intervention_id = body["actions"][0].get("value")
        session = get_session_factory()()
        try:
            rec = accept_intervention(session, intervention_id)
            block = get_calendar_block(session, rec)
            parts = []
            if block is not None:
                parts.append(
                    f"📅 Booked {block.start.strftime('%H:%M')}–{block.end.strftime('%H:%M')} as a wellness walk."
                )
            if rec.baseline_pending:
                parts.append("📱 Run your steps Shortcut now to lock your start, then walk and sync again.")
            else:
                parts.append(
                    f"🚶 Goal: {rec.baseline_steps + (rec.target_steps or 0)} steps. "
                    "Sync your Shortcut when you're back."
                )
            _thread_reply(client, body, " ".join(parts))
        except Exception as exc:
            _thread_reply(client, body, f"Could not start walk: {exc}")
        finally:
            session.close()

    @app.action("mark_done")
    def on_done(ack, body, client):  # noqa: ANN001
        ack()
        from interventions import complete_intervention

        session = get_session_factory()()
        try:
            complete_intervention(session, body["actions"][0].get("value"), reason="employee_done")
            _thread_reply(client, body, "✅ Marked as done.")
        except Exception as exc:
            _thread_reply(client, body, f"Could not mark done: {exc}")
        finally:
            session.close()

    @app.action("remind_later")
    def on_remind(ack, body, client):  # noqa: ANN001
        ack()
        client.chat_postMessage(
            channel=body["channel"]["id"],
            thread_ts=body.get("message", {}).get("thread_ts") or body["message"]["ts"],
            text="Okay — I'll nudge you later within work hours and message caps.",
        )

    @app.action("why")
    def on_why(ack, body, client):  # noqa: ANN001
        ack()
        why = body["actions"][0].get("value") or "See the reasoning in the previous message."
        client.chat_postMessage(
            channel=body["channel"]["id"],
            thread_ts=body.get("message", {}).get("thread_ts") or body["message"]["ts"],
            text=f"*Why this suggestion?*\n{why}",
        )

    # Ack URL buttons / unavailable
    @app.action("order_external")
    def on_external(ack, body):  # noqa: ANN001
        ack()

    return app


def main() -> None:
    setup_logging()
    init_db()
    if not settings.slack_bot_token or not settings.slack_app_token:
        logger.error(
            "Slack tokens missing. Set SLACK_BOT_TOKEN and SLACK_APP_TOKEN. "
            "Simulator works without Slack."
        )
        return
    app = build_app()
    handler = SocketModeHandler(app, settings.slack_app_token)
    logger.info("Slack bot starting (Socket Mode)")
    handler.start()


if __name__ == "__main__":
    main()
