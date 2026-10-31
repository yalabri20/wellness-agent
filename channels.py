"""Channel protocol and shared message helpers (no Slack imports)."""

from __future__ import annotations

from typing import Protocol

from models import ChannelMessage, MessageButton


class Channel(Protocol):
    """Pluggable delivery surface for agent messages."""

    def send_reply(self, user_id: str, message: ChannelMessage) -> str:
        """Send a reply (optionally threaded). Returns message id."""

    def send_proactive(self, user_id: str, message: ChannelMessage) -> str:
        """Send a proactive outreach message. Returns message id."""

    def update_message(self, message_id: str, message: ChannelMessage) -> None:
        """Update a previously sent message (e.g. order status)."""


PLACEHOLDER_URL = "https://example.com/REPLACE_ME"


def is_placeholder_url(url: str | None) -> bool:
    """Return True if URL is missing or still a placeholder."""
    if not url:
        return True
    return "REPLACE_ME" in url or url.rstrip("/") == "https://example.com"


def build_agent_message(
    *,
    text: str,
    thread_id: str | None = None,
    include_confirm: bool = False,
    meal_id: str | None = None,
    meal_url: str | None = None,
    correlation_id: str | None = None,
    why_text: str | None = None,
    start_walk_id: str | None = None,
    intervention_id: str | None = None,
    done_id: str | None = None,
) -> ChannelMessage:
    """Build a ChannelMessage with the standard demo action buttons."""
    buttons: list[MessageButton] = []
    if include_confirm and meal_id:
        buttons.append(
            MessageButton(
                action_id="confirm_snack",
                label="Confirm Recovery Snack",
                style="primary",
                value=meal_id,
            )
        )
        if meal_url and not is_placeholder_url(meal_url):
            buttons.append(
                MessageButton(
                    action_id="order_external",
                    label="Order on HungerStation",
                    url=meal_url,
                )
            )
        else:
            buttons.append(
                MessageButton(
                    action_id="order_external_unavailable",
                    label="Delivery link not configured",
                    style="default",
                    value="unavailable",
                )
            )
    if start_walk_id:
        buttons.append(
            MessageButton(
                action_id="start_walk",
                label="Start walk",
                style="primary",
                value=start_walk_id,
            )
        )
    if done_id:
        buttons.append(
            MessageButton(
                action_id="mark_done",
                label="Done ✓",
                style="primary",
                value=done_id,
            )
        )
    buttons.extend(
        [
            MessageButton(action_id="skip", label="Skip", style="danger"),
            MessageButton(action_id="remind_later", label="Remind me later"),
            MessageButton(
                action_id="why",
                label="Why this suggestion?",
                value=why_text or text,
            ),
        ]
    )
    return ChannelMessage(
        text=text,
        buttons=buttons,
        thread_id=thread_id,
        metadata={
            "correlation_id": correlation_id,
            "meal_id": meal_id,
            "why": why_text,
            "intervention_id": intervention_id or start_walk_id or done_id,
        },
    )


class OutboxChannel:
    """DB-backed channel so the scheduler process can reach the simulator UI."""

    def send_reply(self, user_id: str, message: ChannelMessage) -> str:
        import uuid
        from datetime import datetime

        from db import OutboxMessageRow, get_session_factory

        mid = str(uuid.uuid4())
        session = get_session_factory()()
        try:
            session.add(
                OutboxMessageRow(
                    id=mid,
                    user_id=user_id,
                    payload_json=message.model_dump_json(),
                    created_at=datetime.now().replace(microsecond=0),
                    delivered=False,
                )
            )
            session.commit()
        finally:
            session.close()
        return mid

    def send_proactive(self, user_id: str, message: ChannelMessage) -> str:
        return self.send_reply(user_id, message)

    def update_message(self, message_id: str, message: ChannelMessage) -> None:
        return None


def pull_outbox(user_id: str) -> list[ChannelMessage]:
    """Return undelivered outbox messages for a user and mark them delivered."""
    from sqlalchemy import select

    from db import OutboxMessageRow, get_session_factory

    session = get_session_factory()()
    try:
        rows = session.scalars(
            select(OutboxMessageRow)
            .where(OutboxMessageRow.user_id == user_id, OutboxMessageRow.delivered.is_(False))
            .order_by(OutboxMessageRow.created_at)
        ).all()
        out = [ChannelMessage.model_validate_json(r.payload_json) for r in rows]
        for r in rows:
            r.delivered = True
        session.commit()
        return out
    finally:
        session.close()


class InMemoryChannel:
    """Simple channel that stores messages for the Streamlit simulator / tests."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str, ChannelMessage]] = []
        self._counter = 0

    def send_reply(self, user_id: str, message: ChannelMessage) -> str:
        self._counter += 1
        mid = f"msg_{self._counter}"
        self.messages.append((mid, user_id, message))
        return mid

    def send_proactive(self, user_id: str, message: ChannelMessage) -> str:
        return self.send_reply(user_id, message)

    def update_message(self, message_id: str, message: ChannelMessage) -> None:
        for i, (mid, uid, _) in enumerate(self.messages):
            if mid == message_id:
                self.messages[i] = (mid, uid, message)
                return
