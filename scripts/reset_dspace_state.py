from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import delete, select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import SessionLocal
from app.models import HarvestState


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reset only the DSpace incremental harvest checkpoint."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the reset. Without this flag, runs in dry-run mode.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    session = SessionLocal()
    try:
        state = session.scalar(
            select(HarvestState).where(HarvestState.source_name == "dspace")
        )

        if state is None:
            print("[INFO] No DSpace harvest state exists. Nothing to reset.")
            return 0

        print("[INFO] Current DSpace harvest state found.")
        print(f"[INFO] id={state.id}")
        print(f"[INFO] updated_at={state.updated_at}")
        print(f"[INFO] payload={state.state_payload}")

        if not args.apply:
            print("[DRY-RUN] No changes made. Re-run with --apply to reset this checkpoint.")
            session.rollback()
            return 0

        session.execute(
            delete(HarvestState).where(HarvestState.source_name == "dspace")
        )
        session.commit()
        print("[APPLY] DSpace harvest checkpoint reset successfully.")
        return 0
    except Exception as exc:
        session.rollback()
        print(f"[ERROR] Failed to reset DSpace state: {exc}")
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())

