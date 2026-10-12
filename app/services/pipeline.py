from __future__ import annotations

import logging
import re
from datetime import datetime

from sqlalchemy import case, or_, select, text, update
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.connectors.dspace import DSpaceConnector
from app.connectors.openalex import OpenAlexConnector
from app.connectors.orcid import ORCIDConnector
from app.connectors.scopus import ScopusConnector
from app.connectors.wos import WebOfScienceConnector
from app.database import SessionLocal, engine
from app.models import (
    HarvestRun,
    HarvestState,
    Researcher,
    ResearcherWork,
    SourceRecord,
    Work,
)
from app.schemas import HarmonizedPublication
from app.services.author_names import (
    canonicalize_author_names,
    standardize_openalex_authors,
    standardize_orcid_authors,
)
from app.services.harmonizer import (
    build_dedupe_key,
    is_article_document_type,
    merge_publication_into_work,
    normalize_publication,
)


logger = logging.getLogger(__name__)

ORCID_PATTERN = re.compile(r"\d{4}-\d{4}-\d{4}-[\dXx]{4}")
# Sources whose works can be matched back to OpenAlex after another source has added
# canonical works. The default rebuild path is OpenAlex -> ORCID, so this is mainly
# useful for explicit backfill runs.
ENRICHABLE_SOURCES = ("orcid", "dspace")
# Start with OpenAlex so researcher discovery, validation and mapping are driven by
# OpenAlex authorships that carry verified ORCID iDs. ORCID then enriches those mapped
# researchers' works.
SOURCE_RUN_ORDER = {"openalex": 0, "orcid": 1, "dspace": 2, "scopus": 2, "web_of_science": 2}


class IngestionPipeline:
    def __init__(self) -> None:
        self.connectors = {
            "openalex": OpenAlexConnector(),
            "orcid": ORCIDConnector(),
            "scopus": ScopusConnector(),
            "web_of_science": WebOfScienceConnector(),
            "dspace": DSpaceConnector(),
        }

    def available_sources(self) -> list[str]:
        return ["openalex", "orcid"]

    def default_sources(self) -> list[str]:
        # OpenAlex first, then ORCID enrichment using the researcher ORCID mappings
        # discovered from OpenAlex.
        # DSpace, Scopus and Web of Science remain available via explicit --sources.
        return ["openalex", "orcid"]

    def ingest_sources(
        self,
        source_names: list[str] | None = None,
        limit: int | None = None,
        *,
        article_only: bool = False,
        reset_state: bool = False,
        title_enrichment_limit: int | None = None,
        dspace_enrichment_limit: int | None = None,
    ) -> dict[str, dict]:
        selected_sources = sorted(
            dict.fromkeys(source_names or self.default_sources()),
            key=lambda name: SOURCE_RUN_ORDER.get(name, 1),
        )
        unknown = [name for name in selected_sources if name not in self.connectors]
        if unknown:
            raise ValueError(f"Unknown source(s): {', '.join(unknown)}. Available: {', '.join(self.connectors)}")
        if title_enrichment_limit is None:
            # Backwards compatibility with the old --dspace-enrichment-limit option.
            title_enrichment_limit = dspace_enrichment_limit
        batch_limit = settings.harvest_default_limit if limit is None else limit
        self._ensure_researcher_tables()
        logger.info("Checking database identity integrity")
        self._validate_database_integrity()
        logger.info("Database integrity checks completed")
        if reset_state:
            self._reset_harvest_states(list(selected_sources))
        summary: dict[str, dict] = {}
        for source_name in selected_sources:
            if source_name == "orcid":
                summary[source_name] = self._harvest_orcid_researchers(batch_limit, article_only=article_only)
            elif source_name == "openalex":
                summary[source_name] = self._enrich_with_openalex(
                    batch_limit,
                    article_only=article_only,
                    title_enrichment_limit=title_enrichment_limit,
                )
            else:
                summary[source_name] = self._ingest_single_source(source_name, batch_limit, article_only=article_only)
        self._clear_analytics_cache()
        return summary

    def _ensure_researcher_tables(self) -> None:
        # The API and scheduler do not always call init_db(); create the new tables on demand.
        Researcher.__table__.create(bind=engine, checkfirst=True)
        ResearcherWork.__table__.create(bind=engine, checkfirst=True)

    def _reset_harvest_states(self, source_names: list[str]) -> None:
        session = SessionLocal()
        try:
            if "orcid" in source_names:
                session.execute(
                    update(Researcher).values(orcid_harvested_at=None, orcid_works_last_modified=None)
                )
            if "openalex" in source_names:
                session.execute(update(Researcher).values(openalex_harvested_at=None))
                source_names = [*source_names, "openalex_author_backfill"]
            for source_name in source_names:
                state = session.scalar(
                    select(HarvestState).where(HarvestState.source_name == source_name)
                )
                if state is not None:
                    session.delete(state)
            session.commit()
        finally:
            session.close()

    def _validate_database_integrity(self) -> None:
        duplicate_messages: list[str] = []
        session = SessionLocal()
        try:
            for table_name in (
                "works",
                "source_records",
                "harvest_runs",
                "harvest_states",
                "author_aliases",
                "researchers",
                "researcher_works",
            ):
                logger.info("Checking duplicate IDs in %s", table_name)
                rows = session.execute(
                    text(
                        f"SELECT id, COUNT(*) AS duplicate_count "
                        f"FROM {table_name} "
                        f"GROUP BY id "
                        f"HAVING COUNT(*) > 1 "
                        f"ORDER BY duplicate_count DESC, id ASC "
                        f"LIMIT 10"
                    )
                ).mappings().all()
                if rows:
                    examples = ", ".join(
                        f"id={row['id']} count={row['duplicate_count']}"
                        for row in rows
                    )
                    duplicate_messages.append(f"{table_name}: {examples}")
        finally:
            session.close()

        if duplicate_messages:
            details = "; ".join(duplicate_messages)
            raise RuntimeError(
                "Cannot harvest because database identity columns contain duplicate id values. "
                "SQLAlchemy cannot safely update rows when one ORM identity matches multiple "
                f"database rows. Duplicate examples: {details}. "
                "Inspect with: SELECT id, COUNT(*) FROM works GROUP BY id HAVING COUNT(*) > 1; "
                "Repair the duplicate ids/primary keys before rerunning the harvest."
            )

    def _ingest_single_source(self, source_name: str, limit: int, *, article_only: bool = False) -> dict:
        session = SessionLocal()
        run: HarvestRun | None = None
        run_logging_enabled = True
        try:
            run = HarvestRun(source_name=source_name, status="running")
            session.add(run)
            session.commit()
        except OperationalError as exc:
            session.rollback()
            message = str(exc).lower()
            if "field 'id' doesn't have a default value" in message and "harvest_runs" in message:
                run_logging_enabled = False
            else:
                raise

        try:
            connector = self.connectors[source_name]
            current_state = self._get_source_state(session, source_name)
            fetched_publications = [
                normalize_publication(item)
                for item in connector.fetch(limit=limit, state=current_state)
            ]
            # Compute the incremental-harvest checkpoint (last datestamp/publication date seen)
            # from everything actually fetched, before filtering to articles only. Otherwise,
            # if a filtered-out record happened to be the newest one in the batch, the
            # checkpoint would never advance past it and the harvester would keep re-fetching
            # (and re-discarding) the same trailing non-article records on every run.
            next_state = connector.build_next_state(fetched_publications, current_state)
            publications = (
                [
                    publication
                    for publication in fetched_publications
                    if is_article_document_type(publication.document_type)
                ]
                if article_only
                else fetched_publications
            )
            if source_name == "openalex":
                publications = standardize_openalex_authors(session, publications)
            if source_name == "orcid":
                publications = standardize_orcid_authors(session, publications)
            upserted = self._upsert_publications(session, publications)
            self._save_source_state(session, source_name, next_state)
            if run_logging_enabled and run is not None:
                run.status = "success"
                run.records_fetched = len(fetched_publications)
                run.records_upserted = upserted
                run.finished_at = datetime.utcnow()
                session.commit()
            return {
                "status": "success",
                "records_fetched": len(fetched_publications),
                "records_selected": len(publications),
                "records_upserted": upserted,
            }
        except Exception as exc:
            session.rollback()
            if run_logging_enabled and run is not None:
                run = session.get(HarvestRun, run.id)
                if run is not None:
                    run.status = "failed"
                    run.error_message = str(exc)
                    run.finished_at = datetime.utcnow()
                    session.commit()
            raise
        finally:
            session.close()

    def _upsert_publications(
        self,
        session,
        publications: list[HarmonizedPublication],
        work_ids: list[int] | None = None,
    ) -> int:
        """Upsert publications; when `work_ids` is given, append each publication's work id
        to it in the same order as `publications`."""
        saved = 0
        seen_source_records: dict[tuple[str, str], SourceRecord] = {}

        for index, publication in enumerate(publications, start=1):
            source_key = (publication.source_name, publication.source_record_id)
            source_record = seen_source_records.get(source_key)
            if source_record is None:
                source_record = session.scalar(
                    select(SourceRecord).where(
                        SourceRecord.source_name == publication.source_name,
                        SourceRecord.source_record_id == publication.source_record_id,
                    )
                )

            work = session.get(Work, source_record.work_id) if source_record is not None else None
            if work is None:
                work = self._find_existing_work(session, publication)
            if work is None:
                work = Work(
                    dedupe_key=build_dedupe_key(publication),
                    source_names=[],
                    source_count=0,
                )
                session.add(work)
                session.flush()

            merge_publication_into_work(work, publication)
            if publication.doi:
                work.dedupe_key = build_dedupe_key(publication)
            work.authors = canonicalize_author_names(session, publication.source_name, work.authors)

            if source_record is None:
                source_record = SourceRecord(
                    work_id=work.id,
                    source_name=publication.source_name,
                    source_record_id=publication.source_record_id,
                )
                session.add(source_record)
                session.flush()

            seen_source_records[source_key] = source_record

            source_record.work_id = work.id
            source_record.title = publication.title
            source_record.doi = publication.doi
            source_record.publication_year = publication.publication_year
            source_record.citation_count = publication.citation_count
            source_record.source_url = publication.source_url
            source_record.raw_payload = publication.raw_payload
            source_record.harvested_at = datetime.utcnow()
            saved += 1
            if work_ids is not None:
                work_ids.append(work.id)

            if index % 250 == 0:
                session.commit()
                seen_source_records.clear()

        session.commit()
        return saved

    # ------------------------------------------------------------------ #
    # Run bookkeeping
    # ------------------------------------------------------------------ #
    def _start_run(self, session, source_name: str) -> HarvestRun | None:
        try:
            run = HarvestRun(source_name=source_name, status="running")
            session.add(run)
            session.commit()
            return run
        except OperationalError as exc:
            session.rollback()
            message = str(exc).lower()
            if "field 'id' doesn't have a default value" in message and "harvest_runs" in message:
                return None
            raise

    def _finish_run(self, session, run: HarvestRun | None, *, fetched: int, upserted: int) -> None:
        if run is None:
            return
        run.status = "success"
        run.records_fetched = fetched
        run.records_upserted = upserted
        run.finished_at = datetime.utcnow()
        session.commit()

    def _fail_run(self, session, run: HarvestRun | None, exc: Exception) -> None:
        session.rollback()
        if run is None:
            return
        run = session.get(HarvestRun, run.id)
        if run is not None:
            run.status = "failed"
            run.error_message = str(exc)
            run.finished_at = datetime.utcnow()
            session.commit()

    # ------------------------------------------------------------------ #
    # Step 2: ORCID — enrich OpenAlex-discovered researchers by ORCID iD
    # ------------------------------------------------------------------ #
    def _harvest_orcid_researchers(self, limit: int, *, article_only: bool = False) -> dict:
        session = SessionLocal()
        run = self._start_run(session, "orcid")
        connector: ORCIDConnector = self.connectors["orcid"]
        counters = {
            "researchers_available": 0,
            "researchers_processed": 0,
            "researchers_unchanged": 0,
            "researchers_failed": 0,
            "records_fetched": 0,
            "records_selected": 0,
            "records_upserted": 0,
            "researcher_work_links": 0,
        }
        try:
            researchers = self._researchers_due(
                session,
                Researcher.orcid_harvested_at,
                per_run=settings.orcid_researchers_per_run,
            )
            counters["researchers_available"] = len(researchers)
            logger.info("ORCID: %s OpenAlex-mapped researchers due for enrichment", len(researchers))
            consecutive_failures = 0
            for position, researcher in enumerate(researchers, start=1):
                if limit > 0 and counters["records_fetched"] >= limit:
                    break
                logger.info(
                    "ORCID works %s/%s: %s (%s)",
                    position, len(researchers), researcher.display_name or researcher.orcid, researcher.orcid,
                )
                try:
                    last_modified, fetched = connector.fetch_researcher_works(
                        researcher.orcid,
                        display_name=researcher.display_name,
                    )
                except Exception as exc:  # one bad profile must not stop the whole run
                    session.rollback()
                    counters["researchers_failed"] += 1
                    consecutive_failures += 1
                    logger.warning("ORCID works for %s failed: %s", researcher.orcid, exc)
                    if consecutive_failures >= 20:
                        raise RuntimeError("ORCID harvest aborted after 20 consecutive researcher failures") from exc
                    continue
                consecutive_failures = 0

                if (
                    last_modified is not None
                    and researcher.orcid_harvested_at is not None
                    and researcher.orcid_works_last_modified == last_modified
                ):
                    researcher.orcid_harvested_at = datetime.utcnow()
                    counters["researchers_unchanged"] += 1
                    counters["researchers_processed"] += 1
                    session.commit()
                    continue

                publications = [normalize_publication(item) for item in fetched]
                counters["records_fetched"] += len(publications)
                if article_only:
                    publications = [p for p in publications if is_article_document_type(p.document_type)]
                counters["records_selected"] += len(publications)
                if publications:
                    publications = standardize_orcid_authors(session, publications)
                    work_ids: list[int] = []
                    counters["records_upserted"] += self._upsert_publications(session, publications, work_ids)
                    counters["researcher_work_links"] += self._link_researcher_works(
                        session, researcher.id, work_ids, linked_via="orcid"
                    )
                researcher.orcid_works_last_modified = last_modified
                researcher.orcid_harvested_at = datetime.utcnow()
                counters["researchers_processed"] += 1
                session.commit()

            self._finish_run(
                session, run, fetched=counters["records_fetched"], upserted=counters["records_upserted"]
            )
            return {"status": "success", **counters}
        except Exception as exc:
            self._fail_run(session, run, exc)
            raise
        finally:
            session.close()

    def _researchers_due(self, session, harvested_column, *, per_run: int) -> list[Researcher]:
        """Never-harvested researchers first, then least recently harvested."""
        query = select(Researcher).order_by(
            case((harvested_column.is_(None), 0), else_=1),
            harvested_column.asc(),
            Researcher.id.asc(),
        )
        if per_run > 0:
            query = query.limit(per_run)
        return list(session.scalars(query).all())

    def _upsert_researcher(self, session, person: dict, *, discovered_via: str) -> Researcher | None:
        orcid = self._normalize_orcid(person.get("orcid"))
        if not orcid:
            # Hard rule: a researcher is never recorded without an ORCID iD.
            return None
        researcher = session.scalar(select(Researcher).where(Researcher.orcid == orcid))
        if researcher is None:
            researcher = Researcher(orcid=orcid, discovered_via=discovered_via)
            session.add(researcher)
        for field in ("given_names", "family_name", "credit_name", "display_name"):
            value = person.get(field)
            if value:
                setattr(researcher, field, str(value)[:255])
        if person.get("openalex_author_id") and not researcher.openalex_author_id:
            researcher.openalex_author_id = person["openalex_author_id"]
        if person.get("affiliations"):
            researcher.affiliations = self._merge_values(researcher.affiliations, person["affiliations"])
        session.flush()
        return researcher

    def _link_researcher_works(
        self,
        session,
        researcher_id: int,
        work_ids: list[int],
        *,
        linked_via: str,
    ) -> int:
        wanted = {work_id for work_id in work_ids if work_id}
        if not wanted:
            return 0
        existing = set(
            session.scalars(
                select(ResearcherWork.work_id).where(
                    ResearcherWork.researcher_id == researcher_id,
                    ResearcherWork.work_id.in_(wanted),
                )
            ).all()
        )
        created = 0
        for work_id in sorted(wanted - existing):
            session.add(ResearcherWork(researcher_id=researcher_id, work_id=work_id, linked_via=linked_via))
            created += 1
        if created:
            session.flush()
        return created

    # ------------------------------------------------------------------ #
    # Step 1: OpenAlex — primary harvest and ORCID-keyed researcher mapping
    # ------------------------------------------------------------------ #
    def _enrich_with_openalex(
        self,
        limit: int,
        *,
        article_only: bool = False,
        title_enrichment_limit: int | None = None,
    ) -> dict:
        session = SessionLocal()
        run = self._start_run(session, "openalex")
        connector: OpenAlexConnector = self.connectors["openalex"]
        summary: dict[str, int | str] = {}
        try:
            institution = (
                self._openalex_institution_harvest(session, connector, limit, article_only=article_only)
                if settings.openalex_institution_harvest
                else {"records_fetched": 0, "records_selected": 0, "records_upserted": 0, "researcher_work_links": 0}
            )
            by_researcher = (
                self._openalex_by_researcher(session, connector, limit, article_only=article_only)
                if settings.openalex_researcher_expansion
                else {
                    "researchers_processed": 0,
                    "records_fetched": 0,
                    "records_selected": 0,
                    "records_upserted": 0,
                    "researcher_work_links": 0,
                }
            )
            by_doi = self._openalex_by_doi(
                session,
                connector,
                limit=settings.openalex_doi_enrichment_limit,
                article_only=article_only,
            )
            by_title = self._openalex_by_title(
                session,
                connector,
                self._title_enrichment_limit(limit, override=title_enrichment_limit),
                article_only=article_only,
            )
            session.commit()

            for prefix, part in (
                ("institution", institution),
                ("researcher", by_researcher),
                ("doi", by_doi),
                ("title", by_title),
            ):
                for key, value in part.items():
                    summary[f"{prefix}_{key}"] = value
            fetched = sum(int(part["records_fetched"]) for part in (by_researcher, by_doi, by_title, institution))
            upserted = sum(int(part["records_upserted"]) for part in (by_researcher, by_doi, by_title, institution))
            self._finish_run(session, run, fetched=fetched, upserted=upserted)
            return {"status": "success", **summary, "records_fetched": fetched, "records_upserted": upserted}
        except Exception as exc:
            self._fail_run(session, run, exc)
            raise
        finally:
            session.close()

    def _openalex_by_researcher(
        self,
        session,
        connector: OpenAlexConnector,
        limit: int,
        *,
        article_only: bool,
    ) -> dict[str, int]:
        """1b. For every OpenAlex-discovered ORCID researcher, pull their OpenAlex works by ORCID iD.

        This expands from the institution works into each mapped researcher's OpenAlex
        corpus while still refusing to create researcher mappings without ORCID iDs.
        """
        counters = {
            "researchers_processed": 0,
            "records_fetched": 0,
            "records_selected": 0,
            "records_upserted": 0,
            "researcher_work_links": 0,
        }
        researchers = self._researchers_due(
            session,
            Researcher.openalex_harvested_at,
            per_run=settings.openalex_researchers_per_run,
        )
        logger.info("OpenAlex: %s ORCID-mapped researchers due for researcher-work expansion", len(researchers))
        for position, researcher in enumerate(researchers, start=1):
            if limit > 0 and counters["records_fetched"] >= limit:
                break
            logger.info("OpenAlex by ORCID %s/%s: %s", position, len(researchers), researcher.orcid)
            publications = [normalize_publication(item) for item in connector.fetch_for_orcid(researcher.orcid)]
            counters["records_fetched"] += len(publications)
            if article_only:
                publications = [p for p in publications if is_article_document_type(p.document_type)]
            counters["records_selected"] += len(publications)
            if publications:
                counters["records_upserted"] += self._save_openalex_publications(session, publications, counters)
            researcher.openalex_harvested_at = datetime.utcnow()
            counters["researchers_processed"] += 1
            session.commit()
        return counters

    def _openalex_by_doi(
        self,
        session,
        connector: OpenAlexConnector,
        *,
        limit: int,
        article_only: bool,
    ) -> dict[str, int]:
        """Optional: batch-look-up DOIs of ORCID/DSpace works that still lack OpenAlex data."""
        counters = {"works_checked": 0, "records_fetched": 0, "records_upserted": 0, "researcher_work_links": 0}
        if limit <= 0:
            return counters
        openalex_work_ids = select(SourceRecord.work_id).where(SourceRecord.source_name == "openalex")
        dois = list(
            session.scalars(
                select(Work.doi)
                .join(SourceRecord, SourceRecord.work_id == Work.id)
                .where(SourceRecord.source_name.in_(ENRICHABLE_SOURCES))
                .where(Work.doi.is_not(None))
                .where(Work.id.not_in(openalex_work_ids))
                .distinct()
                .limit(limit)
            ).all()
        )
        logger.info("OpenAlex: %s works with DOIs still need enrichment", len(dois))
        for start in range(0, len(dois), 500):
            chunk = dois[start:start + 500]
            counters["works_checked"] += len(chunk)
            publications = [normalize_publication(item) for item in connector.fetch_by_dois(chunk)]
            counters["records_fetched"] += len(publications)
            if article_only:
                publications = [p for p in publications if is_article_document_type(p.document_type)]
            if publications:
                counters["records_upserted"] += self._save_openalex_publications(session, publications, counters)
            session.commit()
            logger.info("OpenAlex DOI enrichment: %s/%s checked", counters["works_checked"], len(dois))
        return counters

    def _openalex_by_title(
        self,
        session,
        connector: OpenAlexConnector,
        title_limit: int,
        *,
        article_only: bool,
    ) -> dict[str, int]:
        """Optional, slow: title search for ORCID/DSpace works OpenAlex could not match by DOI."""
        counters = {"works_checked": 0, "records_fetched": 0, "records_upserted": 0, "researcher_work_links": 0}
        if title_limit <= 0:
            return counters
        openalex_work_ids = select(SourceRecord.work_id).where(SourceRecord.source_name == "openalex")
        rows = session.execute(
            select(Work, SourceRecord)
            .join(SourceRecord, SourceRecord.work_id == Work.id)
            .where(SourceRecord.source_name.in_(ENRICHABLE_SOURCES))
            .where(Work.id.not_in(openalex_work_ids))
            .order_by(Work.updated_at.desc(), Work.id.desc())
            .limit(title_limit)
        ).all()
        seen_work_ids: set[int] = set()
        for work, record in rows:
            if work.id in seen_work_ids:
                continue
            seen_work_ids.add(work.id)
            counters["works_checked"] += 1
            publication = connector.fetch_for_work(
                doi=work.doi or record.doi,
                title=record.title or work.title,
                year=work.publication_year or record.publication_year,
            )
            if publication is None:
                continue
            counters["records_fetched"] += 1
            if article_only and not is_article_document_type(publication.document_type):
                continue
            if not self._openalex_match_is_for_work(work, record, publication):
                continue
            publication = standardize_openalex_authors(session, [normalize_publication(publication)])[0]
            self._upsert_openalex_enrichment(session, work, publication)
            counters["records_upserted"] += 1
            counters["researcher_work_links"] += self._link_openalex_authorships(session, [publication], [work.id])
            if counters["records_upserted"] % 100 == 0:
                session.commit()
        session.commit()
        return counters

    def _openalex_institution_harvest(
        self,
        session,
        connector: OpenAlexConnector,
        limit: int,
        *,
        article_only: bool,
    ) -> dict[str, int]:
        """1a. Institution-wide OpenAlex harvest. New works are only linked
        to researchers who have an ORCID iD; authors without one are not recorded as researchers."""
        counters = {"records_fetched": 0, "records_selected": 0, "records_upserted": 0, "researcher_work_links": 0}
        logger.info("Starting institution OpenAlex fetch (limit %s)", limit)
        current_state = self._get_source_state(session, "openalex")
        fetched_publications = [
            normalize_publication(item) for item in connector.fetch(limit=limit, state=current_state)
        ]
        next_state = connector.build_next_state(fetched_publications, current_state)
        counters["records_fetched"] = len(fetched_publications)
        publications = (
            [p for p in fetched_publications if is_article_document_type(p.document_type)]
            if article_only
            else fetched_publications
        )
        counters["records_selected"] = len(publications)
        if publications:
            counters["records_upserted"] = self._save_openalex_publications(session, publications, counters)
        self._save_source_state(session, "openalex", next_state)
        session.commit()
        return counters

    def _save_openalex_publications(
        self,
        session,
        publications: list[HarmonizedPublication],
        counters: dict[str, int],
    ) -> int:
        publications = standardize_openalex_authors(session, publications)
        work_ids: list[int] = []
        upserted = self._upsert_publications(session, publications, work_ids)
        counters["researcher_work_links"] = counters.get("researcher_work_links", 0) + self._link_openalex_authorships(
            session, publications, work_ids
        )
        return upserted

    def _link_openalex_authorships(
        self,
        session,
        publications: list[HarmonizedPublication],
        work_ids: list[int],
    ) -> int:
        """Link OpenAlex works to researchers via authorship ORCID iDs.

        - An author whose ORCID iD matches a recorded researcher is linked.
        - An author with an ORCID iD affiliated (on that work) with the configured
          institution is recorded as a new researcher and linked.
        - Authors without an ORCID iD are never recorded as researchers.
        """
        institution_id = str(settings.openalex_institution_id or "").strip().upper()
        wanted: dict[str, dict] = {}
        pairs: list[tuple[str, int]] = []
        for publication, work_id in zip(publications, work_ids, strict=False):
            for authorship in (publication.raw_payload or {}).get("authorships") or []:
                author = authorship.get("author") or {}
                orcid = self._normalize_orcid(author.get("orcid"))
                if not orcid:
                    continue
                at_institution = any(
                    institution_id
                    and str(institution.get("id") or "").upper().rstrip("/").endswith(institution_id)
                    for institution in authorship.get("institutions") or []
                )
                info = wanted.setdefault(
                    orcid,
                    {"display_name": author.get("display_name"), "openalex_id": author.get("id"), "local": False},
                )
                info["local"] = info["local"] or at_institution
                pairs.append((orcid, work_id))
        if not pairs:
            return 0

        researchers: dict[str, Researcher] = {}
        orcids = list(wanted)
        for start in range(0, len(orcids), 500):
            for researcher in session.scalars(
                select(Researcher).where(Researcher.orcid.in_(orcids[start:start + 500]))
            ).all():
                researchers[researcher.orcid] = researcher

        for orcid, info in wanted.items():
            openalex_id = self._openalex_short_id(info["openalex_id"])
            researcher = researchers.get(orcid)
            if researcher is None and info["local"]:
                researcher = self._upsert_researcher(
                    session,
                    {"orcid": orcid, "display_name": info["display_name"], "openalex_author_id": openalex_id},
                    discovered_via="openalex",
                )
                if researcher is not None:
                    researchers[orcid] = researcher
            elif researcher is not None and openalex_id and not researcher.openalex_author_id:
                researcher.openalex_author_id = openalex_id

        work_ids_by_researcher: dict[int, list[int]] = {}
        for orcid, work_id in pairs:
            researcher = researchers.get(orcid)
            if researcher is not None:
                work_ids_by_researcher.setdefault(researcher.id, []).append(work_id)
        return sum(
            self._link_researcher_works(session, researcher_id, ids, linked_via="openalex")
            for researcher_id, ids in work_ids_by_researcher.items()
        )

    def _title_enrichment_limit(self, run_limit: int, *, override: int | None = None) -> int:
        configured_limit = settings.openalex_title_enrichment_limit if override is None else override
        if configured_limit <= 0:
            return 0
        if run_limit <= 0:
            return configured_limit
        return min(run_limit, configured_limit)

    def _normalize_orcid(self, value: str | None) -> str:
        match = ORCID_PATTERN.search(str(value or ""))
        return match.group(0).upper() if match else ""

    def _openalex_short_id(self, value: str | None) -> str | None:
        match = re.search(r"A\d+", str(value or ""), re.IGNORECASE)
        return match.group(0).upper() if match else None

    def _upsert_openalex_enrichment(
        self,
        session,
        work: Work,
        publication: HarmonizedPublication,
    ) -> None:
        source_record = session.scalar(
            select(SourceRecord).where(
                SourceRecord.source_name == publication.source_name,
                SourceRecord.source_record_id == publication.source_record_id,
            )
        )
        if source_record is None:
            source_record = SourceRecord(
                work_id=work.id,
                source_name=publication.source_name,
                source_record_id=publication.source_record_id,
            )
            session.add(source_record)
            session.flush()
        source_record.work_id = work.id
        source_record.title = publication.title
        source_record.doi = publication.doi
        source_record.publication_year = publication.publication_year
        source_record.citation_count = publication.citation_count
        source_record.source_url = publication.source_url
        source_record.raw_payload = publication.raw_payload
        source_record.harvested_at = datetime.utcnow()

        work.abstract = work.abstract or publication.abstract
        work.doi = work.doi or publication.doi
        work.publication_year = work.publication_year or publication.publication_year
        work.publication_date = work.publication_date or publication.publication_date
        work.journal = work.journal or publication.journal
        work.language = work.language or publication.language
        work.publisher = work.publisher or publication.publisher
        work.open_access_status = work.open_access_status or publication.open_access_status
        work.citation_count = max(work.citation_count or 0, publication.citation_count or 0) or None
        work.affiliations = self._merge_values(work.affiliations, publication.affiliations)
        work.keywords = self._merge_values(work.keywords, publication.keywords)
        work.source_names = self._merge_values(work.source_names, [publication.source_name])
        work.source_count = len(work.source_names or [])

    def _merge_values(self, existing: list[str] | None, incoming: list[str] | None) -> list[str]:
        values: list[str] = []
        seen: set[str] = set()
        for value in [*(existing or []), *(incoming or [])]:
            if value is None:
                continue
            text = " ".join(str(value).split())
            key = text.lower()
            if text and key not in seen:
                values.append(text)
                seen.add(key)
        return values

    def _openalex_match_is_for_work(
        self,
        work: Work,
        dspace_record: SourceRecord,
        publication: HarmonizedPublication,
    ) -> bool:
        dspace_doi = self._normalize_doi(work.doi or dspace_record.doi)
        openalex_doi = self._normalize_doi(publication.doi)
        if dspace_doi and openalex_doi:
            return dspace_doi == openalex_doi

        dspace_title = self._title_key(dspace_record.title or work.title)
        openalex_title = self._title_key(publication.title)
        if not dspace_title or not openalex_title:
            return False
        if dspace_title == openalex_title:
            return True
        shorter, longer = sorted((dspace_title, openalex_title), key=len)
        return len(shorter) >= 30 and shorter in longer

    def _normalize_doi(self, value: str | None) -> str:
        text = " ".join(str(value or "").split()).strip().lower()
        text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text)
        text = re.sub(r"^doi:\s*", "", text)
        return text.rstrip(" .")

    def _title_key(self, value: str | None) -> str:
        text = str(value or "").lower()
        text = re.sub(r"[^a-z0-9\s]", " ", text)
        return " ".join(text.split())

    def _get_source_state(self, session, source_name: str) -> dict | None:
        state = session.scalar(
            select(HarvestState).where(HarvestState.source_name == source_name)
        )
        return state.state_payload if state else None

    def _save_source_state(self, session, source_name: str, payload: dict | None) -> None:
        if payload is None:
            return
        state = session.scalar(
            select(HarvestState).where(HarvestState.source_name == source_name)
        )
        if state is None:
            state = HarvestState(source_name=source_name, state_payload=payload)
            session.add(state)
        else:
            state.state_payload = payload

    def _find_existing_work(self, session, publication: HarmonizedPublication) -> Work | None:
        # `dedupe_key` is already a normalized fingerprint: a hash of the DOI when present,
        # otherwise a hash of the normalized title + publication year (see
        # harmonizer.build_dedupe_key). Matching on it covers both cases correctly.
        #
        # The previous version of this method matched on raw `Work.title == publication.title`
        # for DOI-less publications instead of the normalized dedupe_key. That's an exact,
        # case-sensitive string comparison, so the same paper harvested from two different
        # sources (OpenAlex vs. a DSpace repository record, say) with even a trivial
        # difference in title formatting/casing/whitespace never matched, and each was
        # inserted as its own separate Work row. Over four harvested sources, that under-
        # deduplication is what inflates total works/publications well above what any single
        # source (e.g. OpenAlex alone) reports for the same institution.
        dedupe_key = build_dedupe_key(publication)
        filters = [Work.dedupe_key == dedupe_key]
        if publication.doi:
            filters.append(Work.doi == publication.doi)
        return session.scalar(select(Work).where(or_(*filters)))

    def _clear_analytics_cache(self) -> None:
        try:
            from app.api.routes import analytics

            analytics.clear_cache()
        except Exception:
            # Cache clear is a best-effort optimization and should not fail harvest runs.
            return
