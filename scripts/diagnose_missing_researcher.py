"""Explain why a researcher is (or isn't) on the Researchers page, and how to add them.

A researcher is only recorded when an OpenAlex work lists them with BOTH an ORCID iD and a
University of Pretoria affiliation on that work (see IngestionPipeline._link_openalex_authorships).
This script checks each link in that chain:

  1. Is the researcher already in the `researchers` table (maybe under another spelling)?
  2. Which works in our database mention the name, and do their OpenAlex authorships carry
     an ORCID iD and a UP affiliation for this person?
  3. What does OpenAlex itself know about the person (author id, ORCID, UP works)?

Usage:
    python scripts/diagnose_missing_researcher.py --name "Sean Kruger"
    python scripts/diagnose_missing_researcher.py --name "Sean Kruger" --offline   # skip OpenAlex API
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
import unicodedata

import requests
from sqlalchemy import String, cast, or_, select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import settings
from app.connectors.openalex import OpenAlexConnector
from app.database import SessionLocal
from app.models import Researcher, ResearcherWork, SourceRecord, Work

OPENALEX_API = "https://api.openalex.org"


def norm(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())


def name_matches(candidate: str | None, given: str, family: str) -> bool:
    """Family name must match; given name may be full, an initial, or absent."""
    tokens = norm(candidate).split()
    if family not in tokens:
        return False
    if not given:
        return True
    others = [t for t in tokens if t != family]
    return any(t == given or t == given[0] or t.startswith(given) for t in others) or not others


def short_id(value: str | None) -> str:
    return str(value or "").rstrip("/").rsplit("/", 1)[-1]


def bare_orcid(value: str | None) -> str:
    match = re.search(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", str(value or "").upper())
    return match.group(0) if match else ""


def is_up(institution: dict) -> bool:
    wanted = str(settings.openalex_institution_id or "").strip().upper()
    return bool(wanted) and short_id(institution.get("id")).upper() == wanted


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def check_database(given: str, family: str) -> dict:
    found: dict = {"researchers": [], "authorships": []}
    session = SessionLocal()
    try:
        section("1. researchers table")
        rows = session.scalars(
            select(Researcher).where(
                or_(
                    Researcher.display_name.ilike(f"%{family}%"),
                    Researcher.family_name.ilike(f"%{family}%"),
                    Researcher.credit_name.ilike(f"%{family}%"),
                )
            )
        ).all()
        matches = [
            r for r in rows
            if any(name_matches(n, given, family) for n in (
                r.display_name, r.credit_name, f"{r.given_names or ''} {r.family_name or ''}"
            ))
        ]
        if not matches:
            print(f"No researcher named like '{given} {family}' is recorded.")
        for r in matches:
            links = session.scalar(
                select(ResearcherWork.id).where(ResearcherWork.researcher_id == r.id).limit(1)
            )
            print(
                f"FOUND id={r.id} name={r.display_name!r} orcid={r.orcid} "
                f"openalex={r.openalex_author_id} linked_works={'yes' if links else 'NONE'} "
                f"affiliations={(r.affiliations or [])[:2]}"
            )
        found["researchers"] = matches

        section("2. works in our database mentioning the name")
        works = session.scalars(
            select(Work).where(cast(Work.authors, String).ilike(f"%{family}%")).limit(2000)
        ).all()
        works = [w for w in works if any(name_matches(a, given, family) for a in (w.authors or []))]
        print(f"{len(works)} work(s) list an author matching '{given} {family}'.")
        if not works:
            return found

        records = session.execute(
            select(SourceRecord.work_id, SourceRecord.raw_payload)
            .where(SourceRecord.work_id.in_([w.id for w in works]))
            .where(SourceRecord.source_name == "openalex")
        ).all()
        with_orcid_up = with_orcid_only = up_only = neither = 0
        for _, payload in records:
            for authorship in (payload or {}).get("authorships") or []:
                author = authorship.get("author") or {}
                if not name_matches(author.get("display_name"), given, family):
                    continue
                orcid = bare_orcid(author.get("orcid"))
                at_up = any(is_up(i) for i in authorship.get("institutions") or [])
                found["authorships"].append(
                    {"orcid": orcid, "at_up": at_up, "author_id": short_id(author.get("id")),
                     "raw": authorship.get("raw_affiliation_strings") or []}
                )
                if orcid and at_up:
                    with_orcid_up += 1
                elif orcid:
                    with_orcid_only += 1
                elif at_up:
                    up_only += 1
                else:
                    neither += 1
        print(f"OpenAlex records for those works: {len(records)}")
        print(f"  authorships with ORCID + UP affiliation : {with_orcid_up}  <- these create the researcher")
        print(f"  authorships with ORCID, not UP          : {with_orcid_only}")
        print(f"  authorships at UP, no ORCID              : {up_only}")
        print(f"  authorships with neither                 : {neither}")
        samples = {tuple(a["raw"]) for a in found["authorships"] if a["raw"]}
        for raw in list(samples)[:3]:
            print(f"  e.g. affiliation text: {list(raw)[:2]}")
        return found
    finally:
        session.close()


def check_openalex(name: str, given: str, family: str) -> list[dict]:
    section("3. OpenAlex")
    connector = OpenAlexConnector()
    params = connector._with_polite_pool_params({"search": name, "per-page": 10})
    try:
        response = requests.get(
            f"{OPENALEX_API}/authors", params=params, headers=connector._request_headers(), timeout=60
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"Could not reach OpenAlex: {exc}")
        return []
    candidates = []
    for author in response.json().get("results", []):
        if not name_matches(author.get("display_name"), given, family) and not any(
            name_matches(alt, given, family) for alt in author.get("display_name_alternatives") or []
        ):
            continue
        author_id = short_id(author.get("id"))
        up_works = 0
        try:
            count_params = connector._with_polite_pool_params(
                {
                    "filter": f"authorships.author.id:{author_id},institutions.id:{settings.openalex_institution_id}",
                    "per-page": 1,
                }
            )
            count_response = requests.get(
                f"{OPENALEX_API}/works", params=count_params, headers=connector._request_headers(), timeout=60
            )
            count_response.raise_for_status()
            up_works = int(count_response.json().get("meta", {}).get("count") or 0)
        except requests.RequestException:
            up_works = -1
        institutions = [i.get("display_name") for i in author.get("last_known_institutions") or []]
        candidate = {
            "author_id": author_id,
            "name": author.get("display_name"),
            "orcid": bare_orcid(author.get("orcid")),
            "works": author.get("works_count"),
            "up_works": up_works,
            "institutions": institutions,
        }
        candidates.append(candidate)
        print(
            f"{author_id}  {candidate['name']!r}  orcid={candidate['orcid'] or 'NONE'}  "
            f"works={candidate['works']}  UP-affiliated works={up_works if up_works >= 0 else '?'}  "
            f"last known={institutions[:2]}"
        )
    if not candidates:
        print(f"OpenAlex has no author matching '{name}'.")
    return candidates


def verdict(name: str, db: dict, openalex: list[dict]) -> None:
    section("Verdict")
    if db["researchers"]:
        r = db["researchers"][0]
        print(
            f"'{name}' IS recorded as {r.display_name!r} (ORCID {r.orcid}). If the Researchers page "
            "doesn't show them, search for that exact spelling, clear any faculty/year filter "
            "(a faculty filter uses the researcher's own affiliation), and restart the backend."
        )
        return
    if any(a["orcid"] and a["at_up"] for a in db["authorships"]):
        print(
            "Works in the database DO have this person with an ORCID and a UP affiliation, but no "
            "researcher row exists - those works were saved before researcher linking ran. Re-link "
            "them by harvesting the person:"
        )
    up_candidates = [c for c in openalex if c["up_works"] and c["up_works"] > 0]
    for c in up_candidates or openalex:
        if not c["orcid"]:
            print(
                f"- {c['author_id']} ({c['name']}) has NO ORCID iD in OpenAlex, so the pipeline's "
                "'every researcher must have an ORCID' rule skips them. They need to link their "
                "ORCID iD to their works (or claim their OpenAlex profile); then re-harvest."
            )
        elif not c["up_works"]:
            print(
                f"- {c['author_id']} ({c['name']}, ORCID {c['orcid']}) has no works affiliated with UP "
                "in OpenAlex, so they don't qualify as a UP researcher."
            )
        else:
            print(
                f"- {c['author_id']} ({c['name']}, ORCID {c['orcid']}) has {c['up_works']} UP works in "
                "OpenAlex but they aren't harvested yet (the institution harvest hasn't reached them). "
                "Add them now with:\n"
                f"    python scripts/harvest_openalex_researcher.py --openalex-author-id {c['author_id']}"
            )
    if not openalex and not db["authorships"]:
        print("Nothing found in the database or OpenAlex - check the spelling or try --name with only the surname.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", required=True, help="e.g. 'Sean Kruger'")
    parser.add_argument("--offline", action="store_true", help="Skip the OpenAlex API checks.")
    args = parser.parse_args()
    tokens = norm(args.name).split()
    if not tokens:
        raise SystemExit("Empty --name")
    family = tokens[-1]
    given = tokens[0] if len(tokens) > 1 else ""
    db = check_database(given, family)
    openalex = [] if args.offline else check_openalex(args.name, given, family)
    verdict(args.name, db, openalex)


if __name__ == "__main__":
    main()
