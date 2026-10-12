from __future__ import annotations

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import text

from app.database import SessionLocal


TABLES = ("works", "source_records", "harvest_runs", "harvest_states", "author_aliases")


def main() -> int:
    has_errors = False
    session = SessionLocal()
    try:
        for table_name in TABLES:
            print(f"\n[{table_name}]")
            primary_key_rows = session.execute(
                text(f"SHOW KEYS FROM {table_name} WHERE Key_name = 'PRIMARY'")
            ).mappings().all()
            if primary_key_rows:
                columns = ", ".join(str(row["Column_name"]) for row in primary_key_rows)
                print(f"primary_key={columns}")
            else:
                print("primary_key=MISSING")
                has_errors = True

            duplicate_rows = session.execute(
                text(
                    f"SELECT id, COUNT(*) AS duplicate_count "
                    f"FROM {table_name} "
                    f"GROUP BY id "
                    f"HAVING COUNT(*) > 1 "
                    f"ORDER BY duplicate_count DESC, id ASC "
                    f"LIMIT 25"
                )
            ).mappings().all()
            if duplicate_rows:
                has_errors = True
                print("duplicate_ids:")
                for row in duplicate_rows:
                    print(f"  id={row['id']} count={row['duplicate_count']}")
            else:
                print("duplicate_ids=none")
    finally:
        session.close()

    return 1 if has_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
