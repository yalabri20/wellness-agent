"""Headless smoke test of the full demo scenario."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_core import Orchestrator
from channels import InMemoryChannel
from config import DEMO_IMAGES_DIR, get_settings
from db import WalletRow, get_session_factory, init_db, reset_and_seed
from demo_mode import ensure_demo_response_files
from logging_utils import setup_logging
from models import InterventionType
from scheduler import scan_and_notify
from services import confirm_and_create_order, hr_live_feed, hr_metrics, hr_trace


def main() -> int:
    setup_logging()
    ensure_demo_response_files()
    if not (ROOT / "seed" / "employees.json").exists():
        from scripts_gen_seed import main as gen_seed

        gen_seed()
    reset_and_seed()
    init_db()
    settings = get_settings()
    user = settings.live_demo_user_id
    channel = InMemoryChannel()

    print("=== 1) Proactive 12:20 message ===")
    result = scan_and_notify(channel, only_user_id=user, force_demo_1220=True)
    assert result.messages_sent >= 1, result
    _, _, proactive_msg = channel.messages[-1]
    meal_id = proactive_msg.metadata.get("meal_id")
    corr = proactive_msg.metadata.get("correlation_id")
    assert meal_id, "proactive should suggest a catalog snack"
    print(f"  meal_id={meal_id} correlation={corr}")

    session = get_session_factory()()
    try:
        wallet_before = session.get(WalletRow, user).daily_remaining_sar
        order = confirm_and_create_order(
            session,
            user_id=user,
            meal_id=meal_id,
            confirmation_token=f"demo-script-{corr}",
            correlation_id=corr,
            confirmed=True,
        )
        wallet_after = session.get(WalletRow, user).daily_remaining_sar
        assert order.confirmed
        assert wallet_after < wallet_before
        print(f"  order={order.id[:8]}… status={order.status.value}")
        print(f"  wallet {wallet_before} -> {wallet_after} SAR")

        live = hr_live_feed(session, limit=5)
        assert any(r.get("accepted") is True for r in live), live
        print("  live feed shows accepted=True")

        print("=== 2) Heavy lunch photo -> walk ===")
        heavy = (DEMO_IMAGES_DIR / "heavy.jpg").read_bytes()
        orch = Orchestrator(session)
        decision, msg = orch.handle_meal(
            user, image_bytes=heavy, image_key_hint="heavy", text="heavy kabsa lunch"
        )
        assert decision.intervention is not None
        assert decision.intervention.type == InterventionType.WALK, decision.intervention
        assert decision.assessment is not None
        assert decision.assessment.risk_score >= 65
        print(
            f"  risk={decision.assessment.slump_risk} "
            f"score={decision.assessment.risk_score} "
            f"intervention={decision.intervention.type}"
        )
        assert "walk" in msg.text.lower() or "WALK" in msg.text

        print("=== 3) Trace + governance metrics ===")
        traces = hr_trace(session, limit=20)
        assert any(t["agent_name"] == "VisionAgent" for t in traces)
        assert any(t["agent_name"] == "DecisionAgent" for t in traces)
        metrics = hr_metrics(session)
        assert metrics["badge"] == "Simulated data"
        assert metrics["k_anonymity"] >= 5
        print(f"  trace steps={len(traces)} opt_in={metrics['opt_in_rate']}")
        print("SMOKE TEST PASSED")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
