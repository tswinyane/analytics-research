from __future__ import annotations

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.analytics import AnalyticsService


def main() -> None:
    analytics = AnalyticsService()
    rows = analytics.researchers(limit=20)
    print("default_researchers_sample=")
    for row in rows[:10]:
        print(
            f"  {row.get('researcher')}\t"
            f"faculties={row.get('faculties')}\t"
            f"departments={row.get('departments')}\t"
            f"publications={row.get('publication_count')}"
        )

    faculties: list[str] = []
    for row in analytics.researchers(limit=200):
        for faculty in row.get("faculties") or []:
            if faculty and faculty != "Unmapped" and faculty not in faculties:
                faculties.append(faculty)
    print(f"mapped_faculties_in_first_200={faculties[:10]}")

    for faculty in faculties[:5]:
        filtered = analytics.researchers(limit=5, faculty=faculty)
        print(f"filter_faculty={faculty} count_sample={len(filtered)}")
        for row in filtered[:3]:
            print(f"  {row.get('researcher')}\t{row.get('faculties')}\t{row.get('departments')}")


if __name__ == "__main__":
    main()
