"""Which of a researcher's works count towards their University of Pretoria totals?

Two pieces of information decide it:

`researchers.currently_at_institution` - is the researcher at UP now?
    Taken from OpenAlex (the author's last known institutions, or a UP affiliation in the
    current or previous year), falling back to whether their most recent papers list UP.
    It can be pinned by hand (scripts/backfill_link_affiliations.py --set-current), which
    automatic refreshes never overwrite. NULL = not determined yet.

`researcher_works.at_institution` - was the researcher at UP on this particular work?
    * True  - their own OpenAlex authorship on the work lists UP (institution id or a
              "University of Pretoria" affiliation string), or the work is in the UP
              repository (DSpace) and OpenAlex has no authorship for them on it.
    * False - their OpenAlex authorship lists other institutions only, or the work is only
              known from their ORCID profile (ORCID works carry no per-work affiliation).
    * NULL  - not evaluated yet.

A link counts when the researcher is currently at UP (all their works count, including
papers from earlier institutions and collaborations listed under other institutions), or
when they were at UP on that work. Former UP researchers therefore keep only their
UP-affiliated works. NULL link values count, so totals don't collapse before the backfill.
"""
from __future__ import annotations

from datetime import datetime
import logging
import os
import re

import requests
from sqlalchemy import inspect, or_, select, text

from app.config import settings
from app.models import Researcher, ResearcherWork, SourceRecord, Work

logger = logging.getLogger(__name__)

UP_NAME_PATTERNS = (
    "university of pretoria",
    "universiteit van pretoria",
    "univ pretoria",
    "univ. of pretoria",
    "university pretoria",
)
# Units that belong to UP but that OpenAlex (or affiliation text) may list on their own.
# "Onderstepoort Veterinary Institute" is the ARC's, not UP's, so it is deliberately absent.
UP_SUBUNIT_PATTERNS = (
    "forestry and agricultural biotechnology institute",
    "forestry & agricultural biotechnology institute",
    "gordon institute of business science",
    "mammal research institute",
    "onderstepoort veterinary academic hospital",
)
UP_SUBUNIT_WORDS = ("fabi", "gibs")
UP_DISPLAY_NAME = "University of Pretoria"
OPENALEX_AUTHORS_URL = "https://api.openalex.org/authors"

_columns_checked = False

_REQUIRED_COLUMNS = {
    "researcher_works": [
        ("at_institution", "BOOLEAN NULL", "ix_researcher_works_at_institution"),
    ],
    "researchers": [
        ("currently_at_institution", "BOOLEAN NULL", "ix_researchers_currently_at_institution"),
        ("current_affiliation_source", "VARCHAR(20) NULL", None),
        ("current_affiliation_checked_at", "DATETIME NULL", None),
    ],
}


def ensure_researcher_work_affiliation_column(bind) -> None:
    """Add the affiliation columns to existing databases (create_all never alters tables)."""
    global _columns_checked
    if _columns_checked:
        return
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    for table, columns in _REQUIRED_COLUMNS.items():
        if table not in tables:
            continue
        existing = {column["name"] for column in inspector.get_columns(table)}
        for name, ddl, index_name in columns:
            if name in existing:
                continue
            logger.warning("Adding %s.%s column", table, name)
            with bind.begin() as connection:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                if index_name:
                    connection.execute(text(f"CREATE INDEX {index_name} ON {table} ({name})"))
    _columns_checked = True


def counted_link_condition():
    """SQL condition for links that count towards a researcher's UP totals.

    Callers must join `researchers` (Researcher) to `researcher_works` in the query.
    """
    return or_(
        Researcher.currently_at_institution.is_(True),
        ResearcherWork.at_institution.is_(None),
        ResearcherWork.at_institution.is_(True),
    )


# --------------------------------------------------------------------------- #
# Per-work: was the researcher at UP on this work?
# --------------------------------------------------------------------------- #
def _short_id(value) -> str:
    return str(value or "").rstrip("/").rsplit("/", 1)[-1].upper()


def up_institution_ids() -> set[str]:
    """UP's OpenAlex id plus any extra ids for UP units (OPENALEX_UP_INSTITUTION_IDS,
    comma-separated, e.g. FABI's own OpenAlex institution id)."""
    ids = {str(settings.openalex_institution_id or "").strip().upper()}
    ids.update(_short_id(value) for value in os.getenv("OPENALEX_UP_INSTITUTION_IDS", "").split(","))
    return {value for value in ids if value}


def is_up_name(value: str | None) -> bool:
    """Does this institution name / affiliation text refer to UP or one of its units?"""
    lowered = re.sub(r"\s+", " ", str(value or "").lower())
    if not lowered:
        return False
    if any(pattern in lowered for pattern in UP_NAME_PATTERNS + UP_SUBUNIT_PATTERNS):
        return True
    return any(re.search(rf"(?<![a-z]){word}(?![a-z])", lowered) for word in UP_SUBUNIT_WORDS)


def canonical_institution_name(value: str | None) -> str:
    """UP and its units (FABI, GIBS, ...) become one institution: "University of Pretoria"."""
    clean = " ".join(str(value or "").split())
    return UP_DISPLAY_NAME if is_up_name(clean) else clean


def _is_up_institution(institution: dict) -> bool:
    institution = institution or {}
    wanted = up_institution_ids()
    if _short_id(institution.get("id")) in wanted:
        return True
    # OpenAlex lists parent institutions in `lineage` (e.g. a UP institute -> UP).
    if any(_short_id(parent) in wanted for parent in institution.get("lineage") or []):
        return True
    return is_up_name(institution.get("display_name"))


def _mentions_up(values) -> bool:
    return any(is_up_name(value) for value in values or [])


def _authorship_evidence(orcid, openalex_author_id, records) -> bool | None:
    """True/False from the researcher's own OpenAlex authorship on the work, None if none."""
    wanted_orcid = _short_id(orcid)
    wanted_author = _short_id(openalex_author_id)
    found = False
    for source_name, payload in records:
        if source_name != "openalex" or not isinstance(payload, dict):
            continue
        for authorship in payload.get("authorships") or []:
            author = authorship.get("author") or {}
            if not (
                (wanted_orcid and _short_id(author.get("orcid")) == wanted_orcid)
                or (wanted_author and _short_id(author.get("id")) == wanted_author)
            ):
                continue
            found = True
            if any(_is_up_institution(i) for i in authorship.get("institutions") or []):
                return True
            if _mentions_up(authorship.get("raw_affiliation_strings")):
                return True
    return False if found else None


def link_is_at_institution(orcid: str | None, openalex_author_id: str | None, records: list[tuple[str, dict]]) -> bool:
    """records: (source_name, raw_payload) for one work."""
    evidence = _authorship_evidence(orcid, openalex_author_id, records)
    if evidence is not None:
        return evidence
    # In the UP institutional repository and OpenAlex can't say otherwise.
    return any(source_name == "dspace" for source_name, _ in records)


def _records_by_work(session, work_ids: list[int]) -> dict[int, list[tuple[str, dict]]]:
    records: dict[int, list[tuple[str, dict]]] = {}
    for start in range(0, len(work_ids), 500):
        chunk = work_ids[start:start + 500]
        for work_id, source_name, payload in session.execute(
            select(SourceRecord.work_id, SourceRecord.source_name, SourceRecord.raw_payload)
            .where(SourceRecord.work_id.in_(chunk))
            .where(SourceRecord.source_name.in_(("openalex", "dspace")))
        ).all():
            records.setdefault(work_id, []).append((source_name, payload or {}))
    return records


def refresh_link_affiliations(session, researcher_id: int, work_ids: list[int] | None = None) -> dict[str, int]:
    """Recompute at_institution for one researcher's links (all, or just `work_ids`)."""
    ensure_researcher_work_affiliation_column(session.get_bind())
    researcher = session.get(Researcher, researcher_id)
    if researcher is None:
        return {"links": 0, "at_institution": 0, "changed": 0}
    query = select(ResearcherWork).where(ResearcherWork.researcher_id == researcher_id)
    if work_ids is not None:
        wanted = sorted({work_id for work_id in work_ids if work_id})
        if not wanted:
            return {"links": 0, "at_institution": 0, "changed": 0}
        query = query.where(ResearcherWork.work_id.in_(wanted))
    links = session.scalars(query).all()
    if not links:
        return {"links": 0, "at_institution": 0, "changed": 0}

    records_by_work = _records_by_work(session, [link.work_id for link in links])
    changed = 0
    at_up = 0
    for link in links:
        value = link_is_at_institution(
            researcher.orcid,
            researcher.openalex_author_id,
            records_by_work.get(link.work_id, []),
        )
        at_up += int(value)
        if link.at_institution is None or bool(link.at_institution) != value:
            link.at_institution = value
            changed += 1
    if changed:
        # Bumps the Researchers-page cache fingerprint so totals refresh straight away.
        researcher.updated_at = datetime.utcnow()
        session.flush()
    return {"links": len(links), "at_institution": at_up, "changed": changed}


# --------------------------------------------------------------------------- #
# Per-researcher: are they at UP now?
# --------------------------------------------------------------------------- #
def _openalex_current_status(author: dict) -> bool:
    if any(_is_up_institution(i) for i in author.get("last_known_institutions") or []):
        return True
    recent = datetime.utcnow().year - 1
    for affiliation in author.get("affiliations") or []:
        if _is_up_institution(affiliation.get("institution") or {}):
            if max(affiliation.get("years") or [0]) >= recent:
                return True
    return False


def fetch_openalex_current_status(researchers: list[Researcher]) -> dict[int, bool]:
    """researcher.id -> currently at UP, for researchers OpenAlex knows (50 per request)."""
    headers = {"Authorization": f"Bearer {settings.openalex_api_key}"} if settings.openalex_api_key else {}
    base_params = {"mailto": settings.openalex_mailto} if settings.openalex_mailto else {}
    by_author = {_short_id(r.openalex_author_id): r.id for r in researchers if r.openalex_author_id}
    by_orcid = {_short_id(r.orcid): r.id for r in researchers if r.orcid and not r.openalex_author_id}
    result: dict[int, bool] = {}

    def run(filter_name: str, keys: list[str], mapping: dict[str, int], prefix: str) -> None:
        for start in range(0, len(keys), 50):
            chunk = keys[start:start + 50]
            params = {
                **base_params,
                "filter": f"{filter_name}:" + "|".join(f"{prefix}{key}" for key in chunk),
                "per-page": 50,
                "select": "id,orcid,last_known_institutions,affiliations",
            }
            try:
                response = requests.get(OPENALEX_AUTHORS_URL, params=params, headers=headers, timeout=120)
                response.raise_for_status()
            except requests.RequestException as exc:
                logger.warning("OpenAlex author lookup failed (%s); using local evidence for this batch", exc)
                continue
            for author in response.json().get("results", []):
                key = _short_id(author.get("id")) if filter_name == "openalex" else _short_id(author.get("orcid"))
                researcher_id = mapping.get(key)
                if researcher_id is not None:
                    result[researcher_id] = _openalex_current_status(author)

    run("openalex", sorted(by_author), by_author, "")
    run("orcid", sorted(by_orcid), by_orcid, "https://orcid.org/")
    return result


def local_current_status(session, researcher: Researcher) -> bool | None:
    """Fallback: do their most recent papers (by year, where OpenAlex has their
    authorship) list UP? None when we hold no authorship evidence at all."""
    rows = session.execute(
        select(Work.id, Work.publication_year)
        .join(ResearcherWork, ResearcherWork.work_id == Work.id)
        .where(ResearcherWork.researcher_id == researcher.id)
    ).all()
    if not rows:
        return None
    records = _records_by_work(session, [work_id for work_id, _ in rows])
    by_year: dict[int, list[bool]] = {}
    for work_id, year in rows:
        if not year:
            continue
        evidence = _authorship_evidence(researcher.orcid, researcher.openalex_author_id, records.get(work_id, []))
        if evidence is not None:
            by_year.setdefault(int(year), []).append(evidence)
    if not by_year:
        return None
    return any(by_year[max(by_year)])


def refresh_current_status(
    session,
    researcher_ids: list[int] | None = None,
    *,
    use_openalex: bool = True,
    only_unchecked: bool = False,
    checked_before: datetime | None = None,
) -> dict[str, int]:
    """Set researchers.currently_at_institution (manual values are never overwritten)."""
    ensure_researcher_work_affiliation_column(session.get_bind())
    query = select(Researcher).where(
        or_(Researcher.current_affiliation_source.is_(None), Researcher.current_affiliation_source != "manual")
    )
    if researcher_ids is not None:
        query = query.where(Researcher.id.in_(researcher_ids))
    if only_unchecked:
        query = query.where(Researcher.current_affiliation_checked_at.is_(None))
    elif checked_before is not None:
        query = query.where(
            or_(
                Researcher.current_affiliation_checked_at.is_(None),
                Researcher.current_affiliation_checked_at < checked_before,
            )
        )
    researchers = session.scalars(query.order_by(Researcher.id)).all()
    counts = {"checked": 0, "current": 0, "former": 0, "unknown": 0, "changed": 0, "from_openalex": 0}
    for start in range(0, len(researchers), 500):
        batch = researchers[start:start + 500]
        remote = fetch_openalex_current_status(batch) if use_openalex else {}
        now = datetime.utcnow()
        for researcher in batch:
            if researcher.id in remote:
                status, source = remote[researcher.id], "openalex"
                counts["from_openalex"] += 1
            else:
                status, source = local_current_status(session, researcher), "local"
            counts["checked"] += 1
            counts["current" if status else ("former" if status is False else "unknown")] += 1
            if researcher.currently_at_institution != status:
                researcher.currently_at_institution = status
                researcher.updated_at = now  # refreshes the Researchers-page cache
                counts["changed"] += 1
            researcher.current_affiliation_source = source
            researcher.current_affiliation_checked_at = now
        session.commit()
    return counts


def set_current_status_manually(session, orcid: str, value: bool | None) -> Researcher | None:
    """Pin (True/False) or un-pin (None) a researcher's current-UP status."""
    ensure_researcher_work_affiliation_column(session.get_bind())
    researcher = session.scalar(select(Researcher).where(Researcher.orcid == _short_id(orcid)))
    if researcher is None:
        return None
    researcher.currently_at_institution = value
    researcher.current_affiliation_source = "manual" if value is not None else None
    researcher.current_affiliation_checked_at = datetime.utcnow() if value is not None else None
    researcher.updated_at = datetime.utcnow()
    session.commit()
    return researcher
