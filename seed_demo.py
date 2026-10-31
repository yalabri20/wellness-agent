"""Reset the SQLite database to the known demo state."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Ensure project root on path
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import reset_and_seed  # noqa: E402
from demo_mode import ensure_demo_response_files  # noqa: E402
from logging_utils import setup_logging  # noqa: E402

logger = logging.getLogger(__name__)


def main() -> None:
    """Regenerate demo response caches and reseed the database."""
    setup_logging()
    ensure_demo_response_files()
    # Ensure seed JSON exists
    if not (ROOT / "seed" / "employees.json").exists():
        from scripts_gen_seed import main as gen_seed

        gen_seed()
    reset_and_seed()
    logger.info("Demo seed complete.")
    print("OK: database reset to known demo state")


if __name__ == "__main__":
    main()
