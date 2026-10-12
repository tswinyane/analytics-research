from __future__ import annotations

import json
import logging
import re
import threading
import time
import unicodedata
from pathlib import Path

import requests
from sqlalchemy import String, case, cast, func, select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import SessionLocal
from app.models import AuthorAlias, HarvestRun, Researcher, ResearcherWork, SourceRecord, Work
from app.services.faculties import (
    DEPARTMENT_RULES,
    classify_work,
    classify_record,
    department_options,
    faculty_options,
    infer_department,
    infer_faculty,
)

logger = logging.getLogger(__name__)


class AnalyticsService:
    def __init__(self) -> None:
        self._works_cache: list[Work] | None = None
        self._works_cache_expires_at: float = 0.0
        self._works_cache_ttl_seconds = 300.0
        self._dashboard_cache_ttl_seconds = 3600.0
        self._researchers_cache_ttl_seconds = 86400.0
        self._annotated_cache: list[dict] | None = None
        self._annotated_cache_expires_at: float = 0.0
        self._dashboard_rows_cache: list[dict] | None = None
        self._dashboard_rows_cache_expires_at: float = 0.0
        self._dashboard_cache: dict[tuple[str | None, str | None, tuple[int, ...], bool], tuple[float, dict]] = {}
        self._dashboard_cache_lock = threading.Lock()
        self._dashboard_refreshing: set[tuple[str | None, str | None, tuple[int, ...], bool]] = set()
        self._dashboard_cache_file = Path("logs/dashboard_cache.json")
        # Bumped from 8 -> 9: the default (no-filter) dashboard used to hard-code
        # topTopics/topicTrends to empty in _build_default_dashboard_bundle_fast.
        # Bumped from 9 -> 10: researcher_count in that same fast path double-counted
        # authors by summing both "id:"-keyed and "fingerprint:"-keyed AuthorAlias rows.
        # Bumped from 10 -> 11: switched researcher_count off the AuthorAlias table entirely
        # (it's OpenAlex-only and never cleaned up) onto the same author-clustering count the
        # Researchers page uses, so it stays correct as non-article works get cleaned out.
        # Bumped from 11 -> 12: that switch could still cache/persist a placeholder
        # researcher_count of 0 for a full hour if the researcher index wasn't warm yet at the
        # moment the dashboard was requested (e.g. right after a restart, or right after
        # scripts/remove_non_article_works.py invalidated the snapshot) â€” fixed to wait for an
        # in-progress build and to use a short TTL instead of persisting while still building,
        # but any snapshot already saved by the previous version may have 0 baked in.
        # Bumped from 12 -> 13: the default dashboard now computes researcher and
        # collaboration totals directly from Work authorship rows instead of depending on
        # the separate Researchers-page cache or hard-coding collaboration to zero.
        # Bumped from 13 -> 14: the default dashboard researcher KPI now uses the configured
        # OpenAlex institution author count first, with the DSpace primary-creator count as
        # the offline fallback, so collaborators are not counted as UP researchers.
        # Bumped from 14 -> 15: dashboard payload now includes SDG keyword counts for the
        # dedicated SDG keyword block.
        # Bumped from 15 -> 16: discard stale dashboard snapshots that still persisted the
        # pre-fix researcher_count from the researcher list cache.
        # Bumped from 16 -> 17: pin the default institutional researcher KPI to the agreed
        # UP headcount baseline instead of live OpenAlex/DSpace drift.
        # Bumped from 17 -> 18: ensure every default dashboard path, including the slower
        # filtered-builder fallback, uses the same institutional researcher KPI.
        # Bumped from 18 -> 19: runtime analytics no longer calls OpenAlex directly; page
        # requests use the harvested MySQL SourceRecord/AuthorAlias data instead.
        # Bumped from 19 -> 20: Researchers/Publications page data no longer falls back to
        # exported article metadata files or seeded OpenAlex metrics during normal display.
        # Bumped from 20 -> 21: Topic Trends Over Time is limited to 2020 onward.
        # Bumped from 21 -> 22: institutional H-index/collaboration now use the
        # richest harvested local metadata available per work instead of the thin
        # Work authors/affiliations fields, which undercounted DSpace co-authorship.
        # Bumped from 22 -> 23: include citation coverage in the overview so low
        # citation-derived institutional metrics can be tied back to harvest coverage.
        # Bumped from 23 -> 24: reject snapshots created during the old collaboration
        # calculation path, which could persist 0.8% across application restarts.
        # Bumped from 24 -> 25: make the filtered-builder refresh path use the same
        # default institutional metric helpers, so async refreshes after harvest cannot
        # rewrite the default cache with the old Work-only collaboration percentage.
        # Bumped from 25 -> 26: dashboard/researcher rows now append OpenAlex-only
        # works that are missing from the DSpace-shaped row set.
        # Bump the version whenever this payload's *computation* changes (not just its shape)
        # to force a rebuild instead of serving stale numbers for up to an hour (the cache TTL)
        # after every fix.
        # Bumped from 27 -> 28: OpenAlex-only dashboard rows classify faculty/department
        # using ORCID-linked researcher affiliations when available.
        self._dashboard_cache_snapshot_version = 28
        self._dashboard_metric_computation_version = 2
        self._default_institution_researcher_count = 19224
        self._openalex_unmapped_label = "Unmapped"
        self._load_dashboard_cache_snapshot()
        self._dashboard_rows_cache_file = Path("logs/dashboard_rows_cache.json")
        # Bumped from 5 -> 6: discard stale partial row snapshots that only held a tiny
        # historical subset of works, which left researcher faculty/department columns empty.
        # Bumped from 6 -> 7: include OpenAlex-only works alongside DSpace-shaped rows
        # so researcher totals can exceed the DSpace repository subset after enrichment.
        # Bumped from 7 -> 8: OpenAlex-only rows include ORCID-linked researcher
        # affiliations in their faculty/department classification haystack.
        self._dashboard_rows_cache_snapshot_version = 8
        self._researchers_cache_file = Path("logs/researchers_cache.json")
        # Bumped from 9 -> 10: discard stale partial snapshots that only contained a small
        # historical subset of researchers, which broke the Researchers page top ranking.
        # Bumped from 10 -> 11: rebuild researcher "published in" faculties/departments
        # from fresh per-work classification instead of stale dashboard row snapshots.
        # Bumped from 11 -> 12: populate researcher "published in" labels from the article
        # metadata export so the Researchers page keeps those columns without slow rebuilds.
        # Bumped from 12 -> 13: researcher snapshots are MySQL-derived again and can be used
        # as a fast startup cache while a background rebuild refreshes the 90k-record index.
        # Bumped from 13 -> 14: discard sampled researcher snapshots/caches where the FAST
        # fallback counted only the first batch of works, causing list/profile mismatches.
        # Bumped from 14 -> 15: FAST researcher aggregates now include published-in
        # faculties/departments instead of returning empty list-column metadata.
        # Bumped from 15 -> 16: normalize DSpace names like
        # "M.m.s (malie Marie Sophie), 1933 Smuts" so profile/list identity matching
        # uses the real trailing surname instead of the initials/parenthetical prefix.
        # Bumped from 16 -> 17: all-faculty researcher rows now mark the unmapped remainder
        # explicitly when full MySQL totals exceed mapped faculty/department totals.
        # Bumped from 17 -> 18: rename the researcher unmapped remainder label from
        # "Unmapped / OpenAlex-only" to "Unmapped".
        # Bumped from 18 -> 19: merge known reversed-name alias "Gupta Rangan" into
        # canonical "Rangan Gupta" researcher aggregates.
        # Bumped from 19 -> 20: researcher summaries include OpenAlex-only works that
        # were harvested into MySQL but do not exist in DSpace.
        # Bumped from 20 -> 21: researcher summaries inherit the improved
        # OpenAlex-only faculty/department row mapping.
        self._researchers_cache_snapshot_version = 21
        self._dspace_baseline_cache: tuple[float, dict] | None = None
        self._researchers_all_works_cache: tuple[float, list[dict]] | None = None
        self._researchers_all_works_cache_source: str | None = None
        self._researchers_by_author_key: dict[str, dict] = {}
        self._researcher_rows_cache: tuple[float, list[dict]] | None = None
        self._researcher_rows_by_author_key: dict[str, list[dict]] = {}
        self._recent_publications_cache: dict[int, tuple[float, list[dict]]] = {}
        self._filtered_researchers_cache: dict[
            tuple[str | None, str | None, tuple[int, ...], bool],
            tuple[float, list[dict]],
        ] = {}
        self._researcher_profile_cache: dict[
            tuple[str, str | None, str | None, tuple[int, ...], bool],
            tuple[float, dict],
        ] = {}
        self._openalex_institution_author_count_cache: tuple[float, int | None] | None = None
        self._openalex_author_orcid_cache: dict[str, str | None] = {}
        self._openalex_author_metrics_cache: dict[str, dict] = {}
        self._openalex_author_works_cache: dict[str, tuple[float, list[dict]]] = {}
        self._profile_publication_uri_cache: dict[int, str | None] = {}
        self._article_metadata_uri_lookup_cache: tuple[float, dict[str, str]] | None = None
        self._dashboard_metric_cache: dict[str, tuple[float, object]] = {}
        self._openalex_author_timeout_seconds = 8
        self._author_display_aliases = {
            "rangan gupta": "Rangan Gupta",
            "gupta rangan": "Rangan Gupta",
        }
        self._seeded_openalex_author_metrics_by_key = {
            "rangan gupta": {
                "author_id": "A5015669404",
                "publication_count": 1577,
                "citation_count": 31236,
                "h_index": 81,
                "orcid": None,
            },
            "gupta rangan": {
                "author_id": "A5015669404",
                "publication_count": 1577,
                "citation_count": 31236,
                "h_index": 81,
                "orcid": None,
            },
            "sean kruger": {
                "author_id": "A5031250749",
                "publication_count": 29,
                "citation_count": 270,
                "h_index": 5,
                "orcid": "https://orcid.org/0000-0001-6830-8499",
            },
            "kruger sean": {
                "author_id": "A5031250749",
                "publication_count": 29,
                "citation_count": 270,
                "h_index": 5,
                "orcid": "https://orcid.org/0000-0001-6830-8499",
            },
            "hanlie smuts": {
                "author_id": "A5073116394",
                "publication_count": 127,
                "citation_count": 785,
                "h_index": 13,
                "orcid": "https://orcid.org/0000-0001-7120-7787",
            },
            "smuts hanlie": {
                "author_id": "A5073116394",
                "publication_count": 127,
                "citation_count": 785,
                "h_index": 13,
                "orcid": "https://orcid.org/0000-0001-7120-7787",
            },
            "nigel charles bennett": {
                "author_id": "A5045390769",
                "publication_count": 663,
                "citation_count": 18406,
                "h_index": 62,
                "orcid": "https://orcid.org/0000-0001-9748-2947",
            },
            "bennett nigel charles": {
                "author_id": "A5045390769",
                "publication_count": 663,
                "citation_count": 18406,
                "h_index": 62,
                "orcid": "https://orcid.org/0000-0001-9748-2947",
            },
            "nigel bennett": {
                "author_id": "A5045390769",
                "publication_count": 663,
                "citation_count": 18406,
                "h_index": 62,
                "orcid": "https://orcid.org/0000-0001-9748-2947",
            },
            "etheresia pretorius": {
                "author_id": "A5015696712",
                "publication_count": 475,
                "citation_count": 16650,
                "h_index": 66,
                "orcid": "https://orcid.org/0000-0002-9108-2384",
            },
            "pretorius etheresia": {
                "author_id": "A5015696712",
                "publication_count": 475,
                "citation_count": 16650,
                "h_index": 66,
                "orcid": "https://orcid.org/0000-0002-9108-2384",
            },
            "etheresiia pretorius": {
                "author_id": "A5015696712",
                "publication_count": 475,
                "citation_count": 16650,
                "h_index": 66,
                "orcid": "https://orcid.org/0000-0002-9108-2384",
            },
        }
        self._seeded_openalex_author_metrics_by_id = {
            metrics["author_id"]: metrics
            for metrics in self._seeded_openalex_author_metrics_by_key.values()
            if metrics.get("author_id")
        }
        self._seeded_openalex_publications_by_author_id = {
            "A5031250749": [
                {
                    "title": "Transcultural Leadership and Sustainable Development in the Digital Era: Navigating the 4IR in South Africa",
                    "year": 2024,
                    "source": "Society 5.0",
                    "citations": 4,
                    "open_access_status": "unknown",
                    "url": "https://openalex.org/works/W4403339678",
                }
            ],
            "A5073116394": [
                {
                    "title": "Key Characteristics in Selecting Software Tools for Knowledge Management",
                    "year": 2009,
                    "source": "ICEIS 2009 - 11th International Conference on Enterprise Information Systems",
                    "citations": 0,
                    "open_access_status": "unknown",
                    "url": "https://doi.org/10.5220/0001991401700179",
                }
            ],
        }
        self._researcher_orcid_cache: dict[str, str | None] = {}
        self._work_source_title_match_cache: dict[int, bool] = {}
        self._article_metadata_records_cache: tuple[float, list[dict]] | None = None
        self._article_metadata_year_lookup_cache: tuple[float, dict[str, int]] | None = None
        self._article_metadata_article_lookup_cache: tuple[float, set[str]] | None = None
        self._publication_browse_metadata_rows_cache: dict[tuple[str, str, str, str], tuple[float, list[dict]]] = {}
        self._publication_browse_cache_file = Path("logs/publications_browse_cache.json")
        # Bumped from 1 -> 2: include newly harvested OpenAlex-only works after the
        # OpenAlex ingestion step started adding missing institutional records.
        self._publication_browse_cache_snapshot_version = 2
        self._publication_browse_base_rows_cache: tuple[float, list[dict]] | None = None
        self._article_metadata_researcher_counts_cache: tuple[float, dict[str, int]] | None = None
        self._article_metadata_file = Path("outputs/article_metadata_export/article_metadata.json")
        self._last_researchers_cache_status: str = "BYPASS"
        self._researchers_cache_lock = threading.Lock()
        self._researchers_refreshing = False
        self._researcher_rows_cache_lock = threading.Lock()
        self._researcher_rows_refreshing = False
        self._cluster_author_identity_cache: dict[str, tuple[str, str]] = {}
        self._load_researchers_cache_snapshot()
    def dashboard_bundle(
        self,
        faculty: str | None = None,
        department: str | None = None,
        publication_years: list[int] | None = None,
        article_only: bool = False,
    ) -> dict:
        selected_years = self._normalize_publication_years(publication_years)
        cache_key = (faculty, department, selected_years, article_only)
        now = time.monotonic()
        cached = self._dashboard_cache.get(cache_key)
        if cached is not None and now < cached[0]:
            return cached[1]
        if cached is not None:
            self._refresh_dashboard_cache_async(cache_key, faculty, department, selected_years, article_only)
            return cached[1]

        self._dashboard_cache_lock.acquire()
        try:
            cached = self._dashboard_cache.get(cache_key)
            if cached is not None and time.monotonic() < cached[0]:
                return cached[1]
            if (
                (faculty is None or faculty == "All faculties")
                and (department is None or department == "All departments")
                and not selected_years
                and not article_only
            ):
                return self._build_default_dashboard_bundle_fast(cache_key)
            return self._build_dashboard_bundle(faculty, department, selected_years, article_only, cache_key)
        finally:
            self._dashboard_cache_lock.release()

    def _build_default_dashboard_bundle_fast(
        self,
        cache_key: tuple[str | None, str | None, tuple[int, ...], bool],
    ) -> dict:
        now = time.monotonic()
        latest_runs = self.recent_runs(limit=10)
        session = SessionLocal()
        try:
            total_works, citations = session.execute(
                select(
                    func.count(Work.id),
                    func.coalesce(func.sum(Work.citation_count), 0),
                )
            ).one()
            total_works = int(total_works or 0)
            citations = int(citations or 0)
            year_count_rows = session.execute(
                select(Work.publication_year, func.count())
                .where(Work.publication_year.isnot(None))
                .group_by(Work.publication_year)
            ).all()
            document_type_rows = session.execute(
                select(Work.document_type, func.count()).group_by(Work.document_type)
            ).all()
            open_access_rows = session.execute(
                select(Work.open_access_status, func.count()).group_by(Work.open_access_status)
            ).all()
            source_record_counts = session.execute(
                select(SourceRecord.source_name, func.count()).group_by(SourceRecord.source_name)
            ).all()
            top_cited_rows = session.execute(
                select(
                    Work.title,
                    Work.doi,
                    Work.publication_year,
                    Work.citation_count,
                    Work.primary_source_url,
                )
                .order_by(Work.citation_count.desc(), Work.title.asc())
                .limit(10)
            ).all()
            keyword_rows = session.execute(
                select(Work.keywords, Work.publication_year)
                .where(Work.keywords.isnot(None))
                .where(func.JSON_LENGTH(Work.keywords) > 0)
                .order_by(Work.id.desc())
                .limit(10000)
            ).all()
            sdg_keyword_rows = session.execute(
                select(Work.keywords)
                .where(Work.keywords.isnot(None))
                .where(func.JSON_SEARCH(Work.keywords, "all", "%SDG%").isnot(None))
            ).scalars().all()
        finally:
            session.close()

        year_counts = {
            int(year): int(count or 0)
            for year, count in year_count_rows
            if self._normalize_publication_year(year) is not None
        }
        document_type_counts = {
            (document_type or "unknown"): int(count or 0)
            for document_type, count in document_type_rows
        }
        open_access_counts = {
            (open_access_status or "unknown"): int(count or 0)
            for open_access_status, count in open_access_rows
        }
        source_counts = {
            str(source_name or "unknown"): int(count or 0)
            for source_name, count in source_record_counts
        }
        total_source_records = sum(source_counts.values()) or total_works
        citation_metrics = self._default_dashboard_citation_metrics()
        citations = citation_metrics["citation_count"]
        h_index = citation_metrics["h_index"]

        years = sorted(year_counts.keys())
        topic_trend_years = [year for year in years if year >= 2020]
        faculty_counts = self._faculty_counts_from_article_metadata()
        researcher_count = self._default_dashboard_researcher_count()
        collaborative_publications = self._default_dashboard_collaborative_publications()
        collaboration_percentage = (
            round((collaborative_publications / total_works) * 100, 1)
            if total_works
            else 0.0
        )

        # Top keywords / topic trends. This fast path skips the full per-work annotation and
        # clustering pipeline for speed, but it still needs to report topics â€” otherwise the
        # dashboard's default (no-filter) view always shows an empty "Topic Trends Over Time"
        # chart even when the data exists, only ever populating it once a filter forces the
        # slower _build_dashboard_bundle path to run.
        keyword_counts: dict[str, int] = {}
        topic_year_counts: dict[str, dict[int, int]] = {}
        for raw_keywords, keyword_year in keyword_rows:
            normalized_year = self._normalize_publication_year(keyword_year)
            for keyword in raw_keywords or []:
                label = (keyword or "").strip()
                if not label:
                    continue
                keyword_counts[label] = keyword_counts.get(label, 0) + 1
                if normalized_year is not None:
                    yearly = topic_year_counts.setdefault(label, {})
                    yearly[normalized_year] = yearly.get(normalized_year, 0) + 1
        top_keywords = sorted(
            keyword_counts.items(),
            key=lambda x: (-x[1], x[0].lower()),
        )[:12]
        sdg_keyword_counts: dict[str, int] = {}
        for raw_keywords in sdg_keyword_rows:
            for keyword in raw_keywords or []:
                label = " ".join(str(keyword or "").split()).strip()
                if self._is_sdg_keyword(label):
                    sdg_keyword_counts[label] = sdg_keyword_counts.get(label, 0) + 1
        topic_trends = [
            {
                "topic": topic,
                "points": [
                    {"year": y, "count": topic_year_counts.get(topic, {}).get(y, 0)}
                    for y in topic_trend_years
                ],
            }
            for topic, _ in top_keywords[:5]
        ]
        top_cited_publications = [
            {
                "title": row.title or "Untitled publication",
                "doi": row.doi,
                "publication_url": self._publication_url(
                    {"primary_source_url": row.primary_source_url, "doi": row.doi}
                ),
                "publication_year": self._normalize_publication_year(row.publication_year),
                "citation_count": row.citation_count or 0,
            }
            for row in top_cited_rows
        ]

        payload = {
            "overview": {
                "total_works": total_works,
                "total_source_records": total_source_records,
                "duplicates_removed": max(total_source_records - total_works, 0),
                "citation_coverage_publications": citation_metrics["citation_coverage_publications"],
                "metric_computation_version": self._dashboard_metric_computation_version,
                "researcher_count": researcher_count,
                "selected_faculty": "All faculties",
                "selected_department": "All departments",
                "selected_publication_years": [],
                "latest_run": latest_runs[0] if latest_runs else None,
            },
            "kpiSummary": {
                "publications": total_works,
                "citations": citations,
                "downloads": 0,
                "h_index": h_index,
                "researchers": researcher_count,
                "collaboration_percentage": collaboration_percentage,
            },
            "topTopics": [{"topic": k, "count": v} for k, v in top_keywords],
            "sdgKeywords": self._sdg_keywords_from_counts(sdg_keyword_counts),
            "topicTrends": {"years": topic_trend_years, "series": topic_trends},
            "sources": self._rank_counts(source_counts, "source"),
            "facultyBreakdown": self._rank_counts(faculty_counts, "faculty"),
            "years": [{"year": year, "count": year_counts[year]} for year in years],
            "publicationYearOptions": sorted(year_counts.keys(), reverse=True),
            "documentTypes": self._rank_counts(document_type_counts, "document_type"),
            "topCitedPublications": top_cited_publications,
            "openAccess": self._rank_counts(open_access_counts, "open_access_status"),
            "runs": latest_runs,
        }
        self._dashboard_cache[cache_key] = (now + self._dashboard_cache_ttl_seconds, payload)
        self._save_dashboard_cache_snapshot()
        return payload

    def _default_dashboard_researcher_count(self) -> int:
        return self._default_institution_researcher_count

    def _default_dashboard_citation_metrics(self) -> dict[str, int]:
        now = time.monotonic()
        cached = self._dashboard_metric_cache.get("citation_metrics")
        if cached is not None and now < cached[0]:
            return cached[1]  # type: ignore[return-value]

        session = SessionLocal()
        try:
            source_citations = (
                select(
                    SourceRecord.work_id.label("work_id"),
                    func.max(SourceRecord.citation_count).label("source_citation_count"),
                )
                .where(SourceRecord.citation_count.isnot(None))
                .group_by(SourceRecord.work_id)
                .subquery()
            )
            work_citations = func.coalesce(Work.citation_count, 0)
            harvested_citations = func.coalesce(source_citations.c.source_citation_count, 0)
            best_citation_count = case(
                (harvested_citations > work_citations, harvested_citations),
                else_=work_citations,
            )
            rows = session.execute(
                select(best_citation_count)
                .select_from(Work)
                .outerjoin(source_citations, source_citations.c.work_id == Work.id)
            ).all()
        finally:
            session.close()

        citation_counts = sorted((int(row[0] or 0) for row in rows), reverse=True)
        h_index = 0
        for index, citation_count in enumerate(citation_counts, start=1):
            if citation_count >= index:
                h_index = index
            else:
                break
        metrics = {
            "citation_count": sum(citation_counts),
            "h_index": h_index,
            "citation_coverage_publications": sum(1 for count in citation_counts if count > 0),
        }
        self._dashboard_metric_cache["citation_metrics"] = (
            now + self._dashboard_cache_ttl_seconds,
            metrics,
        )
        return metrics

    def _default_dashboard_collaborative_publications(self) -> int:
        now = time.monotonic()
        cached = self._dashboard_metric_cache.get("collaborative_publications")
        if cached is not None and now < cached[0]:
            return int(cached[1])

        collaborative_work_ids: set[int] = set()

        session = SessionLocal()
        try:
            work_rows = session.execute(select(Work.id, Work.authors, Work.affiliations)).all()
            for work_id, authors, affiliations in work_rows:
                if self._work_authorship_is_collaborative(authors, affiliations):
                    collaborative_work_ids.add(int(work_id))

            source_rows = session.execute(
                select(SourceRecord.work_id, SourceRecord.source_name, SourceRecord.raw_payload)
                .where(SourceRecord.source_name.in_(("dspace", "openalex")))
            ).all()
        finally:
            session.close()

        for work_id, source_name, payload in source_rows:
            if int(work_id) in collaborative_work_ids:
                continue
            if source_name == "dspace" and len(self._dspace_creators(payload or {})) > 1:
                collaborative_work_ids.add(int(work_id))
            elif source_name == "openalex" and self._openalex_payload_is_collaborative(payload or {}):
                collaborative_work_ids.add(int(work_id))

        collaborative_publications = len(collaborative_work_ids)
        self._dashboard_metric_cache["collaborative_publications"] = (
            now + self._dashboard_cache_ttl_seconds,
            collaborative_publications,
        )
        return collaborative_publications

    def _work_authorship_is_collaborative(self, authors: list | None, affiliations: list | None) -> bool:
        author_entries = self._iter_author_entries(authors or [])
        affiliation_keys = {
            affiliation.strip().lower()
            for affiliation in affiliations or []
            if isinstance(affiliation, str) and affiliation.strip()
        }
        return len(author_entries) > 1 or len(affiliation_keys) > 1

    def _openalex_payload_is_collaborative(self, payload: dict) -> bool:
        author_count = 0
        institution_keys: set[str] = set()
        for authorship in payload.get("authorships") or []:
            author = authorship.get("author") or {}
            if author.get("display_name") or author.get("id"):
                author_count += 1
            for institution in authorship.get("institutions") or []:
                name = " ".join(str(institution.get("display_name") or "").split()).strip()
                if name:
                    institution_keys.add(name.lower())
        return author_count > 1 or len(institution_keys) > 1

    def _sdg_keywords_from_counts(self, keyword_counts: dict[str, int], limit: int | None = None) -> list[dict]:
        sdg_rows = [
            {"keyword": keyword, "count": count}
            for keyword, count in keyword_counts.items()
            if self._is_sdg_keyword(keyword)
        ]
        sdg_rows.sort(key=lambda item: (-item["count"], item["keyword"].lower()))
        return sdg_rows if limit is None else sdg_rows[:limit]

    def _is_sdg_keyword(self, keyword: str | None) -> bool:
        text = " ".join(str(keyword or "").split())
        if not text:
            return False
        return re.search(r"\bSDG\s*[-:]?\s*(?:[0-9]{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\b", text, re.IGNORECASE) is not None

    def _build_dashboard_bundle(
        self,
        faculty: str | None,
        department: str | None,
        selected_years: tuple[int, ...],
        article_only: bool,
        cache_key: tuple[str | None, str | None, tuple[int, ...], bool],
    ) -> dict:
        now = time.monotonic()
        dashboard_rows_all = self._dashboard_rows()
        year_option_rows = self._filter_annotated_works(
            dashboard_rows_all,
            faculty,
            department,
            article_only,
        )
        publication_year_options = sorted(
            {
                self._work_value(item["work"], "publication_year")
                for item in year_option_rows
                if self._work_value(item["work"], "publication_year") is not None
            },
            reverse=True,
        )
        annotated = self._filter_annotated_works(
            dashboard_rows_all,
            faculty,
            department,
            article_only,
            selected_years,
        )
        clustered_rows = self._cluster_dashboard_rows(annotated)
        latest_runs = self.recent_runs(limit=10)

        total_works = 0
        total_source_records = 0
        citations = 0

        year_counts = {}
        source_counts = {}
        keyword_counts = {}
        faculty_counts = {}
        document_type_counts = {}
        open_access_counts = {}

        researcher_index = {}

        topic_year_counts = {}
        collaborative_publications = 0

        for item in clustered_rows:
            work = item["work"]

            total_works += 1
            total_source_records += self._work_value(work, "source_count") or 0
            citations += self._work_value(work, "citation_count") or 0

            # Year
            year = self._work_value(work, "publication_year")
            if year is not None:
                year_counts[year] = year_counts.get(year, 0) + 1

            # Sources
            for s in self._work_list(work, "source_names"):
                source_counts[s] = source_counts.get(s, 0) + 1

            # Keywords + topic trends
            for k in self._work_list(work, "keywords"):
                k = k.strip()
                if not k:
                    continue

                keyword_counts[k] = keyword_counts.get(k, 0) + 1

                if year is not None:
                    if k not in topic_year_counts:
                        topic_year_counts[k] = {}
                    topic_year_counts[k][year] = topic_year_counts[k].get(year, 0) + 1

            # Faculty
            f = self._visible_mapping_label(item["faculty"])
            if f:
                faculty_counts[f] = faculty_counts.get(f, 0) + 1

            # Document type
            dt = self._work_value(work, "document_type") or "unknown"
            document_type_counts[dt] = document_type_counts.get(dt, 0) + 1

            # Open access
            oa = self._work_value(work, "open_access_status") or "unknown"
            open_access_counts[oa] = open_access_counts.get(oa, 0) + 1

            # Researchers
            for author in self._iter_author_entries(self._work_list(work, "authors")):
                author_key, author_display = self._cluster_author_identity(author)
                if not author_key:
                    continue
                entry = researcher_index.setdefault(
                    author_key,
                    {
                        "researcher": author_display,
                        "publication_count": 0,
                        "citation_count": 0,
                    },
                )
                entry["researcher"] = self._pick_better_author_name(entry["researcher"], author_display)
                entry["publication_count"] += 1
                entry["citation_count"] += self._work_value(work, "citation_count") or 0

            if self._is_collaborative_dashboard_work(work):
                collaborative_publications += 1

        use_dspace_baseline = (
            (faculty is None or faculty == "All faculties")
            and (department is None or department == "All departments")
            and not selected_years
            and not article_only
        )
        if use_dspace_baseline:
            researcher_count = self._default_dashboard_researcher_count()
            citation_metrics = self._default_dashboard_citation_metrics()
            citations = citation_metrics["citation_count"]
            h_index = citation_metrics["h_index"]
            collaborative_publications = min(
                self._default_dashboard_collaborative_publications(),
                total_works,
            )
            citation_coverage_publications = citation_metrics["citation_coverage_publications"]
        else:
            researcher_count = len(researcher_index)
            collaborative_publications = min(collaborative_publications, total_works)

        # ---- Derived Metrics ----

        duplicates_removed = max(total_source_records - total_works, 0)

        # H-index
        citation_list = sorted(
            (self._work_value(item["work"], "citation_count") or 0 for item in annotated),
            reverse=True,
        )
        if not use_dspace_baseline:
            h_index = 0
            for i, c in enumerate(citation_list, start=1):
                if c >= i:
                    h_index = i
                else:
                    break
            citation_coverage_publications = sum(1 for value in citation_list if value > 0)

        # Researchers
        researchers_sorted = sorted(
            [
                {
                    "researcher": v["researcher"],
                    "publication_count": v["publication_count"],
                    "citation_count": v["citation_count"],
                }
                for v in researcher_index.values()
            ],
            key=lambda x: (-x["publication_count"], -x["citation_count"]),
        )

        top_cited_publications = sorted(
            [
                {
                    "title": self._work_value(item["work"], "title") or "Untitled publication",
                    "doi": self._work_value(item["work"], "doi"),
                    "publication_url": self._publication_url(item["work"]),
                    "publication_year": self._work_value(item["work"], "publication_year"),
                    "citation_count": self._work_value(item["work"], "citation_count") or 0,
                }
                for item in annotated
            ],
            key=lambda x: (-x["citation_count"], x["title"].lower()),
        )[:10]

        # Top keywords
        top_keywords = sorted(
            keyword_counts.items(),
            key=lambda x: (-x[1], x[0].lower()),
        )[:12]

        top_keyword_names = [k for k, _ in top_keywords]

        # Topic trends
        years = sorted(year_counts.keys())
        topic_trend_years = [year for year in years if year >= 2020]
        topic_trends = []

        for topic in top_keyword_names[:5]:
            yearly = topic_year_counts.get(topic, {})
            topic_trends.append({
                "topic": topic,
                "points": [{"year": y, "count": yearly.get(y, 0)} for y in topic_trend_years],
            })

        collaboration_percentage = (
            round((collaborative_publications / total_works) * 100, 1)
            if total_works
            else 0.0
        )

        # ---- Build Response ----

        payload = {
            "overview": {
                "total_works": total_works,
                "total_source_records": total_source_records,
                "duplicates_removed": duplicates_removed,
                "citation_coverage_publications": citation_coverage_publications,
                "metric_computation_version": self._dashboard_metric_computation_version,
                "researcher_count": researcher_count,
                "selected_faculty": faculty or "All faculties",
                "selected_department": department or "All departments",
                "selected_publication_years": list(selected_years),
                "latest_run": latest_runs[0] if latest_runs else None,
            },
            "kpiSummary": {
                "publications": total_works,
                "citations": citations,
                "downloads": 0,
                "h_index": h_index,
                "researchers": researcher_count,
                "collaboration_percentage": collaboration_percentage,
            },
            "topTopics": [{"topic": k, "count": v} for k, v in top_keywords],
            "sdgKeywords": self._sdg_keywords_from_counts(keyword_counts),
            "topicTrends": {
                "years": topic_trend_years,
                "series": topic_trends,
            },
            "sources": [{"source": k, "count": v} for k, v in sorted(source_counts.items(), key=lambda x: -x[1])],
            "facultyBreakdown": [{"faculty": k, "count": v} for k, v in
                                 sorted(faculty_counts.items(), key=lambda x: -x[1])],
            "years": [{"year": y, "count": year_counts[y]} for y in years],
            "publicationYearOptions": publication_year_options,
            "documentTypes": [{"document_type": k, "count": v} for k, v in
                              sorted(document_type_counts.items(), key=lambda x: -x[1])],
            "topCitedPublications": top_cited_publications,
            "openAccess": [{"open_access_status": k, "count": v} for k, v in
                           sorted(open_access_counts.items(), key=lambda x: -x[1])],
            "runs": latest_runs,
        }
        self._dashboard_cache[cache_key] = (now + self._dashboard_cache_ttl_seconds, payload)
        self._save_dashboard_cache_snapshot()
        return payload

    def _refresh_dashboard_cache_async(
        self,
        cache_key: tuple[str | None, str | None, tuple[int, ...], bool],
        faculty: str | None,
        department: str | None,
        selected_years: tuple[int, ...],
        article_only: bool,
    ) -> None:
        if cache_key in self._dashboard_refreshing:
            return
        self._dashboard_refreshing.add(cache_key)

        def refresh() -> None:
            try:
                with self._dashboard_cache_lock:
                    if (
                        (faculty is None or faculty == "All faculties")
                        and (department is None or department == "All departments")
                        and not selected_years
                        and not article_only
                    ):
                        self._build_default_dashboard_bundle_fast(cache_key)
                    else:
                        self._build_dashboard_bundle(faculty, department, selected_years, article_only, cache_key)
            finally:
                self._dashboard_refreshing.discard(cache_key)

        threading.Thread(target=refresh, daemon=True).start()

    def overview(
        self,
        faculty: str | None = None,
        department: str | None = None,
        article_only: bool = False,
    ) -> dict:
        if (
            (faculty is None or faculty == "All faculties")
            and (department is None or department == "All departments")
            and not article_only
        ):
            return self.dashboard_bundle()["overview"]
        annotated = self._filter_annotated_works(self._annotated_works(), faculty, department, article_only)
        return self._overview_from_annotated(annotated, faculty, department)

    def faculties(self) -> list[str]:
        return faculty_options()

    def departments(self, faculty: str | None = None) -> list[str]:
        return department_options(faculty)

    def by_source(
        self,
        faculty: str | None = None,
        department: str | None = None,
        article_only: bool = False,
    ) -> list[dict]:
        annotated = self._filter_annotated_works(self._annotated_works(), faculty, department, article_only)
        return self._by_source_from_annotated(annotated)

    def by_year(
        self,
        faculty: str | None = None,
        department: str | None = None,
        article_only: bool = False,
    ) -> list[dict]:
        annotated = self._filter_annotated_works(self._annotated_works(), faculty, department, article_only)
        return self._by_year_from_annotated(annotated)

    def by_faculty(self) -> list[dict]:
        return self._by_faculty_from_annotated(self._annotated_works())

    def by_department(self, faculty: str | None = None, article_only: bool = False) -> list[dict]:
        annotated = self._filter_annotated_works(self._annotated_works(), faculty, None, article_only)
        return self._by_department_from_annotated(annotated)

    def by_document_type(
        self,
        faculty: str | None = None,
        department: str | None = None,
        article_only: bool = False,
    ) -> list[dict]:
        annotated = self._filter_annotated_works(self._annotated_works(), faculty, department, article_only)
        return self._by_document_type_from_annotated(annotated)

    def top_journals(
        self,
        limit: int = 10,
        faculty: str | None = None,
        department: str | None = None,
        article_only: bool = False,
    ) -> list[dict]:
        annotated = self._filter_annotated_works(self._annotated_works(), faculty, department, article_only)
        return self._top_journals_from_annotated(annotated, limit)

    def top_authors(
        self,
        limit: int = 10,
        faculty: str | None = None,
        department: str | None = None,
        article_only: bool = False,
    ) -> list[dict]:
        if (faculty is None or faculty == "All faculties") and (department is None or department == "All departments") and not article_only:
            return self._researchers_from_all_works(limit)
        rows = self._filter_annotated_works(self._dashboard_rows(), faculty, department, article_only)
        return self._researchers_from_dashboard_rows(rows, limit)

    def researchers(
        self,
        limit: int | None = 25,
        faculty: str | None = None,
        department: str | None = None,
        publication_years: list[int] | tuple[int, ...] | None = None,
        article_only: bool = False,
        researcher: str | None = None,
    ) -> list[dict]:
        researcher_query = " ".join(str(researcher or "").split()).strip()
        if (
            (faculty is None or faculty == "All faculties")
            and (department is None or department == "All departments")
            and not article_only
            and not publication_years
        ):
            rows = self._limit_researcher_results(
                self._filter_researcher_results(
                    self._researchers_from_all_works(
                        None,
                        allow_sync_rebuild=False,
                    ),
                    researcher_query,
                ),
                limit,
            )
            if self._last_researchers_cache_status != "FAST":
                rows = self._enrich_researchers_with_openalex_metrics(rows)
            return self._rank_researcher_results(rows, researcher_query)
        self._last_researchers_cache_status = "BYPASS"
        if self._is_openalex_unmapped_filter(faculty, department):
            researcher_rows = self._openalex_unmapped_researcher_rows(
                researcher_query,
                publication_years,
                article_only,
            )
        elif publication_years:
            self._last_researchers_cache_status = "FAST"
            researcher_rows = self._filter_researcher_results(
                self._researchers_from_source_records_fast(
                    None,
                    faculty=faculty,
                    department=department,
                    publication_years=publication_years,
                    article_only=article_only,
                ),
                researcher_query,
            )
        else:
            researcher_rows = self._filter_researcher_results(
                self._researchers_from_article_metadata(
                    faculty,
                    department,
                    publication_years,
                    article_only,
                ),
                researcher_query,
            )
        rows = self._limit_researcher_results(researcher_rows, limit)
        return self._rank_researcher_results(rows, researcher_query)

    def researchers_browse(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        faculty: str | None = None,
        department: str | None = None,
        publication_years: list[int] | tuple[int, ...] | None = None,
        article_only: bool = False,
        researcher: str | None = None,
        alphabet: str | None = None,
    ) -> dict:
        page = max(int(page or 1), 1)
        page_size = min(max(int(page_size or 20), 1), 100)
        selected_letter = str(alphabet or "").strip().lower()[:1]
        if selected_letter and not selected_letter.isalpha():
            selected_letter = ""

        rows = self.researchers(
            limit=None,
            faculty=faculty,
            department=department,
            publication_years=publication_years,
            article_only=article_only,
            researcher=researcher,
        )
        is_default_institution_view = (
            (faculty is None or faculty == "All faculties")
            and (department is None or department == "All departments")
            and not publication_years
            and not article_only
            and not " ".join(str(researcher or "").split()).strip()
        )
        if is_default_institution_view:
            rows = rows[: self._default_dashboard_researcher_count()]
        if selected_letter:
            rows = [
                row
                for row in rows
                if self._researcher_sort_name(row.get("researcher")).startswith(selected_letter)
            ]
            rows = sorted(rows, key=lambda row: self._researcher_sort_name(row.get("researcher")))

        total = len(rows)
        total_pages = max((total + page_size - 1) // page_size, 1)
        current_page = min(page, total_pages)
        start = (current_page - 1) * page_size
        return {
            "rows": rows[start : start + page_size],
            "total": total,
            "page": current_page,
            "page_size": page_size,
            "total_pages": total_pages,
        }

    def research_fields(
        self,
        limit: int = 25,
        faculty: str | None = None,
        department: str | None = None,
        publication_years: list[int] | tuple[int, ...] | None = None,
        article_only: bool = False,
    ) -> list[dict]:
        selected_years = self._normalize_publication_years(publication_years)
        cached_fields = self._research_fields_from_dashboard_cache(
            faculty,
            department,
            selected_years,
            article_only,
            limit,
        )
        if cached_fields is not None:
            return cached_fields

        annotated = self._filter_annotated_works(
            self._dashboard_rows(),
            faculty,
            department,
            article_only,
            selected_years,
        )
        counts: dict[str, int] = {}
        for item in annotated:
            for keyword in item["work"].get("keywords") or []:
                label = keyword.strip()
                if label:
                    counts[label] = counts.get(label, 0) + 1
        return self._rank_counts(counts, "field", limit)

    def _research_fields_from_dashboard_cache(
        self,
        faculty: str | None,
        department: str | None,
        publication_years: tuple[int, ...],
        article_only: bool,
        limit: int,
    ) -> list[dict] | None:
        cache_key = (faculty, department, publication_years, article_only)
        cached = self._dashboard_cache.get(cache_key)
        if cached is None:
            return None
        payload = cached[1]
        top_topics = payload.get("topTopics")
        if not isinstance(top_topics, list):
            return None
        fields: list[dict] = []
        for item in top_topics[:limit]:
            if not isinstance(item, dict):
                continue
            label = item.get("topic")
            count = item.get("count")
            if label:
                fields.append({"field": label, "count": count or 0})
        return fields

    def researcher_profile(
        self,
        researcher: str,
        faculty: str | None = None,
        department: str | None = None,
        publication_years: list[int] | tuple[int, ...] | None = None,
        article_only: bool = False,
        publication_title: str | None = None,
    ) -> dict:
        selected_years = self._normalize_publication_years(publication_years)
        publication_title_query = self._normalize_search_text(publication_title)
        cache_key = (researcher, faculty, department, selected_years, article_only, publication_title_query)
        now = time.monotonic()
        cached = self._researcher_profile_cache.get(cache_key)
        if cached is not None and now < cached[0]:
            return cached[1]

        target_key, target_display = self._cluster_author_identity(researcher)
        target_raw = researcher.strip().lower()
        use_metadata_profile_rows = (
            not selected_years
            and (
                (faculty is not None and faculty != "All faculties")
                or (department is not None and department != "All departments")
            )
            and not self._is_openalex_unmapped_filter(faculty, department)
        )
        if use_metadata_profile_rows:
            researcher_rows = self._researcher_profile_rows_from_metadata(
                researcher=researcher,
                target_key=target_key,
                target_raw=target_raw,
                faculty=faculty,
                department=department,
                article_only=article_only,
                publication_title=publication_title,
            )
        else:
            researcher_rows = self._researcher_profile_rows(
                researcher=researcher,
                target_key=target_key,
                target_raw=target_raw,
                faculty=faculty,
                department=department,
                publication_years=selected_years,
                article_only=article_only,
                publication_title=publication_title,
            )
        raw_researcher_rows = researcher_rows
        researcher_rows = self._cluster_researcher_profile_rows(researcher_rows)
        listing_summary = self._researcher_profile_summary_from_rows(raw_researcher_rows)
        profile_display = (
            (listing_summary.get("researcher") or "").strip()
            if listing_summary is not None
            else ""
        ) or target_display or researcher

        publication_count = (
            listing_summary["publication_count"]
            if listing_summary is not None
            else len(researcher_rows)
        )
        citations_total = (
            listing_summary["citation_count"]
            if listing_summary is not None
            else sum((item["work"].get("citation_count") or 0) for item in researcher_rows)
        )
        citation_counts = sorted(
            ((item["work"].get("citation_count") or 0) for item in researcher_rows),
            reverse=True,
        )
        h_index = 0
        for idx, value in enumerate(citation_counts, start=1):
            if value >= idx:
                h_index = idx
            else:
                break
        if listing_summary is not None and listing_summary.get("h_index") is not None:
            h_index = listing_summary["h_index"]
        openalex_metrics = (
            self._researcher_openalex_metrics(
                target_key,
                target_raw,
                researcher_rows,
                allow_live=False,
            )
            if publication_title_query
            else None
        )
        if openalex_metrics is not None:
            publication_count = max(
                int(publication_count or 0),
                int(openalex_metrics.get("publication_count") or 0),
            )
            citations_total = openalex_metrics.get("citation_count", citations_total)
            h_index = openalex_metrics.get("h_index", h_index)
        openalex_publication_rows: list[dict] = []
        if openalex_metrics is not None:
            author_id = openalex_metrics.get("author_id")
            if publication_title_query:
                openalex_publication_rows = self._openalex_author_publication_rows_for_title(
                    author_id,
                    publication_title_query,
                )
            else:
                openalex_publication_rows = self._openalex_author_publication_rows(author_id, allow_live=False)

        yearly_publications: dict[int, int] = {}
        yearly_citations: dict[int, int] = {}
        coauthor_counts: dict[str, int] = {}
        institution_counts: dict[str, int] = {}
        keyword_counts: dict[str, int] = {}
        subject_area_counts: dict[str, int] = {}
        open_access_counts: dict[str, int] = {}
        publication_rows: list[dict] = []

        for item in researcher_rows:
            work = item["work"]
            year = self._normalize_publication_year(work.get("publication_year"))
            citations = work.get("citation_count") or 0
            if year is not None:
                yearly_publications[year] = yearly_publications.get(year, 0) + 1
                yearly_citations[year] = yearly_citations.get(year, 0) + citations

            for author in self._iter_author_entries(work.get("authors") or []):
                author_key, author_display = self._cluster_author_identity(author)
                if not author_key:
                    continue
                if target_key and author_key == target_key:
                    continue
                if not target_key and author_display.lower() == target_raw:
                    continue
                coauthor_counts[author_display] = coauthor_counts.get(author_display, 0) + 1

            for affiliation in work.get("affiliations") or []:
                clean_affiliation = (affiliation or "").strip()
                if clean_affiliation:
                    institution_counts[clean_affiliation] = institution_counts.get(clean_affiliation, 0) + 1

            for keyword in work.get("keywords") or []:
                clean_keyword = (keyword or "").strip()
                if clean_keyword:
                    keyword_counts[clean_keyword] = keyword_counts.get(clean_keyword, 0) + 1

            subject_area = (work.get("document_type") or "unknown").strip()
            subject_area_counts[subject_area] = subject_area_counts.get(subject_area, 0) + 1

            oa_status = (work.get("open_access_status") or "unknown").strip().lower()
            open_access_counts[oa_status] = open_access_counts.get(oa_status, 0) + 1

            publication_rows.append(
                {
                    "title": work.get("title") or "Untitled publication",
                    "year": year,
                    "source": ", ".join(work.get("source_names") or []) or "Unknown source",
                    "citations": citations,
                    "open_access_status": (work.get("open_access_status") or "Unknown"),
                    "_work": work,
                }
            )

        if publication_title_query:
            publication_rows = [
                row
                for row in publication_rows
                if publication_title_query in self._normalize_search_text(row.get("title"))
            ]
            openalex_publication_rows = [
                row
                for row in openalex_publication_rows
                if publication_title_query in self._normalize_search_text(row.get("title"))
            ]

        if openalex_publication_rows:
            publication_rows = self._merge_publication_rows(publication_rows, openalex_publication_rows)
        if len(openalex_publication_rows) > len(researcher_rows):
            yearly_publications = {}
            yearly_citations = {}
            open_access_counts = {}
            for row in publication_rows:
                year = self._normalize_publication_year(row.get("year"))
                citations = row.get("citations") or 0
                if year is not None:
                    yearly_publications[year] = yearly_publications.get(year, 0) + 1
                    yearly_citations[year] = yearly_citations.get(year, 0) + citations
                oa_status = (row.get("open_access_status") or "unknown").strip().lower()
                open_access_counts[oa_status] = open_access_counts.get(oa_status, 0) + 1

        publication_rows.sort(
            key=lambda row: (row["citations"], row["year"] if row["year"] is not None else -1),
            reverse=True,
        )
        visible_publication_rows = publication_rows[:25]
        visible_publication_uris = self._profile_publication_uris(
            [row.get("_work") for row in visible_publication_rows if row.get("_work") is not None]
        )
        for row in visible_publication_rows:
            work = row.pop("_work", None)
            if not row.get("uri") and work is not None:
                uri = visible_publication_uris.get(id(work)) or self._publication_url(work)
                if uri:
                    row["uri"] = uri
                    row["url"] = uri
            elif row.get("uri") and not row.get("url"):
                row["url"] = row["uri"]

        all_years = sorted(set(yearly_publications.keys()) | set(yearly_citations.keys()))
        publications_over_time = [{"year": y, "count": yearly_publications.get(y, 0)} for y in all_years]
        citations_over_time = [{"year": y, "count": yearly_citations.get(y, 0)} for y in all_years]

        top_affiliations = [
            {"institution": label, "count": count}
            for label, count in sorted(
                institution_counts.items(),
                key=lambda pair: (-pair[1], pair[0].lower()),
            )[:10]
        ]
        top_coauthors = [
            {"name": label, "count": count}
            for label, count in sorted(
                coauthor_counts.items(),
                key=lambda pair: (-pair[1], pair[0].lower()),
            )[:15]
        ]
        top_keywords = [
            {"keyword": label, "count": count}
            for label, count in sorted(
                keyword_counts.items(),
                key=lambda pair: (-pair[1], pair[0].lower()),
            )[:20]
        ]
        top_subject_areas = [
            {"subject_area": label, "count": count}
            for label, count in sorted(
                subject_area_counts.items(),
                key=lambda pair: (-pair[1], pair[0].lower()),
            )[:10]
        ]

        open_statuses = {"open", "gold", "green", "bronze", "hybrid", "repository"}
        open_access_total = sum(
            count for status, count in open_access_counts.items() if status in open_statuses
        )
        total_open_access_known = sum(open_access_counts.values())
        non_open_access_total = max(total_open_access_known - open_access_total, 0)
        open_access_percentage = (
            round((open_access_total / total_open_access_known) * 100, 1)
            if total_open_access_known
            else 0.0
        )
        profile_orcid = (
            openalex_metrics.get("orcid")
            if openalex_metrics is not None
            else self._orcid_from_openalex_author_search(target_key, researcher)
        )

        profile = {
            "researcher": profile_display,
            "orcid": profile_orcid,
            "affiliations": top_affiliations,
            "quick_stats": {
                "publications": publication_count,
                "citations": citations_total,
                "h_index": h_index,
            },
            "performance_overview": {
                "publications_over_time": publications_over_time,
                "citations_over_time": citations_over_time,
            },
            "open_access_overview": {
                "open_access_count": open_access_total,
                "non_open_access_count": non_open_access_total,
                "total_publications": total_open_access_known,
                "open_access_percentage": open_access_percentage,
            },
            "publications": visible_publication_rows,
            "collaboration": {
                "coauthors": top_coauthors,
                "institutions": top_affiliations,
                "countries": [],
            },
            "research_areas": {
                "keywords": top_keywords,
                "subject_areas": top_subject_areas,
            },
        }
        profile_cache_ttl = self._dashboard_cache_ttl_seconds if profile["orcid"] else 60.0
        self._researcher_profile_cache[cache_key] = (
            time.monotonic() + profile_cache_ttl,
            profile,
        )
        return profile

    def _merge_publication_rows(self, primary_rows: list[dict], secondary_rows: list[dict]) -> list[dict]:
        merged: dict[str, dict] = {}
        order: list[str] = []
        for row in [*primary_rows, *secondary_rows]:
            key = self._normalize_search_text(row.get("title"))
            if not key:
                continue
            existing = merged.get(key)
            if existing is None:
                merged[key] = dict(row)
                order.append(key)
                continue
            existing["citations"] = max(existing.get("citations") or 0, row.get("citations") or 0)
            if not existing.get("year") and row.get("year"):
                existing["year"] = row["year"]
            if (existing.get("source") or "Unknown source") == "Unknown source" and row.get("source"):
                existing["source"] = row["source"]
            if not existing.get("url") and row.get("url"):
                existing["url"] = row["url"]
            if not existing.get("uri") and row.get("uri"):
                existing["uri"] = row["uri"]
            if (existing.get("open_access_status") or "unknown").lower() == "unknown" and row.get("open_access_status"):
                existing["open_access_status"] = row["open_access_status"]
        return [merged[key] for key in order]

    def _researcher_listing_summary(
        self,
        *,
        target_key: str,
        target_raw: str,
        faculty: str | None,
        department: str | None,
        publication_years: tuple[int, ...],
        article_only: bool,
    ) -> dict | None:
        for item in self.researchers(
            limit=None,
            faculty=faculty,
            department=department,
            publication_years=publication_years,
            article_only=article_only,
            researcher=target_raw,
        ):
            author_key, author_display = self._cluster_author_identity(item.get("researcher"))
            if target_key and author_key == target_key:
                return item
            if not target_key and author_display.strip().lower() == target_raw:
                return item
        return None

    def _researcher_profile_summary_from_rows(self, rows: list[dict]) -> dict:
        citation_count = 0
        journal_names: set[str] = set()
        latest_year: int | None = None
        for item in rows:
            work = item.get("work") or {}
            citation_count += work.get("citation_count") or 0
            journal = (work.get("journal") or "").strip()
            if journal:
                journal_names.add(journal)
            year = self._normalize_publication_year(work.get("publication_year"))
            if year is not None and (latest_year is None or year > latest_year):
                latest_year = year
        return {
            "publication_count": len(rows),
            "citation_count": citation_count,
            "journal_count": len(journal_names),
            "latest_year": latest_year,
        }

    def _cluster_researcher_profile_rows(self, rows: list[dict]) -> list[dict]:
        clustered: dict[str, dict] = {}
        title_clustered: dict[str, dict] = {}
        unkeyed: list[dict] = []
        for item in rows:
            key = self._publication_cluster_key(item["work"])
            title_key = self._publication_title_cluster_key(item["work"])
            if not key and not title_key:
                unkeyed.append(item)
                continue
            existing = clustered.get(key) if key else None
            if existing is None and title_key:
                existing = title_clustered.get(title_key)
            if existing is None:
                if key:
                    clustered[key] = item
                if title_key:
                    title_clustered[title_key] = item
                continue

            existing_work = existing["work"]
            incoming_work = item["work"]
            existing_work["citation_count"] = max(
                self._work_value(existing_work, "citation_count") or 0,
                self._work_value(incoming_work, "citation_count") or 0,
            )
            for field in ("source_names", "authors", "keywords", "affiliations"):
                existing_work[field] = list(
                    {
                        *(self._work_list(existing_work, field)),
                        *(self._work_list(incoming_work, field)),
                    }
                )
            for field in ("primary_source_url", "open_access_status", "document_type", "journal"):
                if not self._work_value(existing_work, field):
                    existing_work[field] = self._work_value(incoming_work, field)
            if self._work_value(existing_work, "publication_year") is None:
                existing_work["publication_year"] = self._work_value(incoming_work, "publication_year")
            if key:
                clustered[key] = existing
            if title_key:
                title_clustered[title_key] = existing
        deduped: list[dict] = []
        seen_rows: set[int] = set()
        for item in clustered.values():
            row_id = id(item)
            if row_id in seen_rows:
                continue
            seen_rows.add(row_id)
            deduped.append(item)
        return [*deduped, *unkeyed]

    def open_access_breakdown(
        self,
        faculty: str | None = None,
        department: str | None = None,
        article_only: bool = False,
    ) -> list[dict]:
        annotated = self._filter_annotated_works(self._annotated_works(), faculty, department, article_only)
        return self._open_access_from_annotated(annotated)

    def _article_metadata_records(self) -> list[dict]:
        now = time.monotonic()
        cached = self._article_metadata_records_cache
        if cached is not None and now < cached[0]:
            return cached[1]
        try:
            with self._article_metadata_file.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return []
        records = payload.get("records") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            return []
        self._article_metadata_records_cache = (
            now + self._dashboard_cache_ttl_seconds,
            records,
        )
        return records

    def _faculty_counts_from_article_metadata(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self._article_metadata_records():
            faculty = self._visible_mapping_label(record.get("Faculty"))
            if faculty:
                counts[faculty] = counts.get(faculty, 0) + 1
        return counts

    def _mapped_publication_counts_by_author_key_from_article_metadata(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self._article_metadata_records():
            faculty = self._visible_mapping_label(record.get("Faculty"))
            department = self._visible_mapping_label(record.get("Department"))
            if not faculty and not department:
                continue
            author_keys = self._metadata_author_alias_keys(record.get("Main Author"))
            if not author_keys:
                continue
            for author_key in author_keys:
                counts[author_key] = counts.get(author_key, 0) + 1
        return counts

    def _metadata_author_alias_keys(self, author_name: str | None) -> set[str]:
        author_key, _ = self._cluster_author_identity(author_name)
        keys = {author_key} if author_key else set()
        parts = [part.strip() for part in str(author_name or "").split(",", 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            for candidate in (f"{parts[0]} {parts[1]}", f"{parts[1]} {parts[0]}"):
                candidate_key, _ = self._cluster_author_identity(candidate)
                if candidate_key:
                    keys.add(candidate_key)
        return keys

    def _mapped_publication_count_for_researcher(self, researcher_name: str | None) -> int:
        normalized_name = self._normalize_search_text(researcher_name)
        if not normalized_name:
            return 0
        now = time.monotonic()
        cached = self._article_metadata_researcher_counts_cache
        if cached is None or now >= cached[0]:
            counts: dict[str, int] = {}
            for row in self._researchers_from_dashboard_rows(self._filter_researcher_rows(), None):
                publication_count = int(row.get("publication_count") or 0)
                researcher = row.get("researcher")
                author_key, _ = self._cluster_author_identity(researcher)
                if not author_key:
                    continue
                counts[author_key] = max(counts.get(author_key, 0), publication_count)
                display_key = self._normalize_search_text(researcher)
                if display_key:
                    counts[display_key] = max(counts.get(display_key, 0), publication_count)
            cached = (now + self._dashboard_cache_ttl_seconds, counts)
            self._article_metadata_researcher_counts_cache = cached
        author_key, _ = self._cluster_author_identity(researcher_name)
        return max(
            int(cached[1].get(normalized_name) or 0),
            int(cached[1].get(author_key) or 0),
        )

    def _append_openalex_unmapped_label_if_needed(
        self,
        researcher: dict,
        openalex_publication_count: int | None,
    ) -> dict:
        if openalex_publication_count is None:
            return researcher
        mapped_count = self._mapped_publication_count_for_researcher(researcher.get("researcher"))
        if int(openalex_publication_count or 0) <= mapped_count:
            return researcher
        updated = dict(researcher)
        for key in ("faculties", "departments"):
            values = list(updated.get(key) or [])
            if self._openalex_unmapped_label not in values:
                values.append(self._openalex_unmapped_label)
            updated[key] = values
        updated["mapped_publication_count"] = mapped_count
        updated["openalex_publication_count"] = int(openalex_publication_count or 0)
        return updated

    def _researcher_profile_rows_from_metadata(
        self,
        *,
        researcher: str,
        target_key: str,
        target_raw: str,
        faculty: str | None,
        department: str | None,
        article_only: bool,
        publication_title: str | None = None,
    ) -> list[dict]:
        search_term = self._researcher_search_term(researcher, target_key).lower()
        if not search_term:
            return []
        publication_title_query = self._normalize_search_text(publication_title)

        rows: list[dict] = []
        for record in self._article_metadata_records():
            title = record.get("Article Title") or "Untitled publication"
            if publication_title_query and publication_title_query not in self._normalize_search_text(title):
                continue
            main_author = record.get("Main Author") or ""
            if search_term not in main_author.lower():
                continue
            authors = self._iter_author_entries([main_author])
            if not self._author_matches_profile(
                authors,
                target_key,
                target_raw,
            ) and not self._metadata_author_name_matches_profile(main_author, target_raw):
                continue

            mapped_faculty = record.get("Faculty") or "Unmapped"
            mapped_department = record.get("Department") or "Unmapped"
            if faculty and faculty != "All faculties" and mapped_faculty != faculty:
                continue
            if department and department != "All departments" and mapped_department != department:
                continue

            institution_text = "; ".join(
                str(record.get(key) or "")
                for key in ("Main Institution", "Collaborating Institution", "Institution Affiliation")
            )
            affiliations = [value.strip() for value in institution_text.split(";") if value.strip()]
            rows.append(
                {
                    "work": {
                        "id": None,
                        "title": title,
                        "doi": record.get("DOI") or None,
                        "journal": None,
                        "authors": authors,
                        "affiliations": affiliations,
                        "keywords": [],
                        "publication_year": None,
                        "document_type": "Article" if article_only else None,
                        "open_access_status": "repository",
                        "citation_count": 0,
                        "primary_source_url": record.get("Article URI") or None,
                        "source_names": ["dspace"],
                    },
                    "faculty": mapped_faculty,
                    "department": mapped_department,
                }
            )
        return rows

    def _filter_researcher_rows(
        self,
        faculty: str | None = None,
        department: str | None = None,
        publication_years: list[int] | tuple[int, ...] | None = None,
        article_only: bool = False,
    ) -> list[dict]:
        selected_years = set(self._normalize_publication_years(publication_years))
        rows: list[dict] = []
        for item in self._researcher_rows():
            work = item.get("work") or {}
            if faculty and faculty != "All faculties" and item.get("faculty") != faculty:
                continue
            if department and department != "All departments" and item.get("department") != department:
                continue
            if article_only and not self._is_article_work(work):
                continue
            if selected_years:
                year = self._normalize_publication_year(work.get("publication_year"))
                if year not in selected_years:
                    continue
            rows.append(item)
        return rows

    def _researcher_profile_rows_from_combined_index(
        self,
        *,
        researcher: str,
        target_key: str,
        target_raw: str,
        faculty: str | None,
        department: str | None,
        article_only: bool,
    ) -> list[dict]:
        search_term = self._researcher_search_term(researcher, target_key).lower()
        if not search_term:
            return []

        if not self._researcher_rows_cache_ready():
            self._researcher_rows()

        rows: list[dict] = []
        source_rows = self._researcher_rows_by_author_key.get(target_key) if target_key else None
        if source_rows is None:
            source_rows = self._researcher_rows()
        for item in source_rows:
            work = item.get("work") or {}
            authors = self._iter_author_entries(work.get("authors") or [])
            if not any(search_term in str(author).lower() for author in authors):
                continue
            if not self._author_matches_profile(authors, target_key, target_raw):
                continue
            if not self._work_title_matches_source_record(work):
                continue
            if faculty and faculty != "All faculties" and item["faculty"] != faculty:
                continue
            if department and department != "All departments" and item["department"] != department:
                continue
            if article_only and not self._is_article_work(work):
                continue
            rows.append(item)
        return rows

    def _researcher_profile_rows(
        self,
        *,
        researcher: str,
        target_key: str,
        target_raw: str,
        faculty: str | None,
        department: str | None,
        publication_years: tuple[int, ...],
        article_only: bool,
        publication_title: str | None = None,
    ) -> list[dict]:
        search_term = self._researcher_search_term(researcher, target_key)
        if not search_term:
            return []

        filters = [cast(Work.authors, String).ilike(f"%{search_term}%")]
        clean_publication_title = " ".join(str(publication_title or "").split()).strip()
        if clean_publication_title:
            filters.append(cast(Work.title, String).ilike(f"%{clean_publication_title}%"))

        session = SessionLocal()
        try:
            works = session.execute(
                select(Work)
                .where(*filters)
                .order_by(Work.publication_year.desc(), Work.id.desc())
                .limit(5000)
            ).scalars().all()
        finally:
            session.close()

        selected_years = set(publication_years)
        rows: list[dict] = []
        for work in works:
            authors = self._iter_author_entries(work.authors or [])
            if not self._author_matches_profile(authors, target_key, target_raw):
                continue

            mapped_faculty, mapped_department = classify_work(work)

            item = {
                "work": {
                    "id": work.id,
                    "title": work.title,
                    "doi": work.doi,
                    "journal": work.journal,
                    "authors": authors,
                    "affiliations": work.affiliations or [],
                    "keywords": work.keywords or [],
                    "publication_year": work.publication_year,
                    "document_type": work.document_type,
                    "open_access_status": work.open_access_status,
                    "citation_count": work.citation_count,
                    "primary_source_url": work.primary_source_url,
                    "source_names": work.source_names or ["dspace"],
                },
                "faculty": mapped_faculty,
                "department": mapped_department,
            }

            if faculty and faculty != "All faculties" and item["faculty"] != faculty:
                continue
            if department and department != "All departments" and item["department"] != department:
                continue
            if article_only and not self._is_article_work(item["work"]):
                continue
            if selected_years:
                year = self._normalize_publication_year(item["work"].get("publication_year"))
                if year not in selected_years:
                    continue
            rows.append(item)

        return rows

    def _researcher_search_term(self, researcher: str, target_key: str) -> str:
        if target_key and "|" in target_key:
            return target_key.split("|", 1)[0]
        cleaned = " ".join(str(researcher or "").split()).strip()
        cleaned = re.sub(r"\(\s*\)", "", cleaned).strip(" ,")
        if not cleaned:
            return ""
        if "," in cleaned:
            return cleaned.split(",", 1)[0].strip()
        tokens = [token.strip(" .") for token in cleaned.split() if token.strip(" .")]
        return tokens[-1] if tokens else ""

    def _author_matches_profile(self, authors: list[str], target_key: str, target_raw: str) -> bool:
        target_surname = ""
        target_given_signature = ""
        if target_key and "|" in target_key:
            target_surname, target_given_signature = target_key.split("|", 1)
        for author in authors:
            author_key, author_display = self._cluster_author_identity(author)
            if target_key and author_key == target_key:
                return True
            if target_key and author_key:
                continue
            if not target_key and author_display.strip().lower() == target_raw:
                return True
            if target_surname and len(target_given_signature) == 1:
                normalized = unicodedata.normalize("NFKD", str(author))
                normalized = normalized.encode("ascii", "ignore").decode("ascii").lower()
                normalized = re.sub(r"\b\d{4}\s*[-â€“]\s*\d{0,4}\b", " ", normalized)
                normalized = re.sub(r"[^a-z\s-]", " ", normalized)
                tokens = [token for token in normalized.split() if token]
                if len(tokens) <= 3 and (
                    (
                        tokens[-1] == target_surname
                        and any(token.startswith(target_given_signature) for token in tokens[:-1])
                    )
                    or (
                        tokens[0] == target_surname
                        and any(token.startswith(target_given_signature) for token in tokens[1:])
                    )
                ):
                    return True
        return False

    def publications(
        self,
        limit: int = 100,
        faculty: str | None = None,
        department: str | None = None,
        publication_years: list[int] | tuple[int, ...] | None = None,
        article_only: bool = True,
    ) -> list[dict]:
        if (
            (faculty is None or faculty == "All faculties")
            and (department is None or department == "All departments")
            and not publication_years
            and article_only
        ):
            publication_rows = self._recent_publications_from_dspace_records_only_fast(limit)
            if not publication_rows:
                publication_rows = self._recent_publications_fast(limit)
        else:
            publication_rows = self._recent_publications_from_metadata_export(
                limit=limit,
                faculty=faculty,
                department=department,
                publication_years=publication_years,
            )
            if not publication_rows:
                publication_rows = self._recent_publications_from_source_records_fast(
                    limit=limit,
                    faculty=faculty,
                    department=department,
                    publication_years=publication_years,
                    article_only=article_only,
                )

        if not publication_rows:
            publication_rows = self._recent_publications_from_works_fast(limit, article_only=article_only)
        if not publication_rows and not article_only:
            publication_rows = self._recent_publications_from_works_fast(limit, article_only=False)
        if not publication_rows:
            publication_rows = self._recent_publications_from_source_records_fast(
                limit=limit,
                faculty=faculty,
                department=department,
                publication_years=publication_years,
                article_only=article_only,
            )
        return publication_rows

    def _recent_publications_from_dspace_records_only_fast(self, limit: int) -> list[dict]:
        now = time.monotonic()
        cache_key = max(int(limit or 100), 1)
        cached = self._recent_publications_cache.get(cache_key)
        if cached is not None and now < cached[0]:
            return cached[1]

        fetch_limit = max(cache_key * 5, 500)
        session = SessionLocal()
        try:
            records = session.execute(
                select(SourceRecord)
                .where(SourceRecord.source_name == "dspace")
                .order_by(SourceRecord.publication_year.desc(), SourceRecord.id.desc())
                .limit(fetch_limit)
            ).scalars().all()
        finally:
            session.close()

        publications: list[dict] = []
        seen_keys: set[str] = set()
        for record in records:
            payload = record.raw_payload if isinstance(record.raw_payload, dict) else {}
            if not self._is_article_document_type(self._source_record_document_type(payload)):
                continue
            title = record.title or self._source_record_title(payload)
            if not title:
                continue
            doi = record.doi or self._source_record_doi(payload)
            doi_key = self._normalize_doi_key(doi)
            title_key = self._title_fingerprint(title)
            row_keys = [
                key
                for key in (
                    f"doi:{doi_key}" if doi_key else "",
                    f"title:{title_key}" if title_key else "",
                    f"source:{record.source_record_id}",
                )
                if key
            ]
            if any(key in seen_keys for key in row_keys):
                continue
            seen_keys.update(row_keys)

            faculty, department = self._dspace_faculty_department(
                dspace_payload=payload,
                title=title,
                journal=None,
                publisher=None,
                keywords=self._dspace_values(payload, "subject"),
            )
            faculty_label = self._visible_mapping_label(faculty)
            department_label = self._visible_mapping_label(department)
            if not faculty_label or not department_label:
                continue

            publication_url = self._dspace_record_url(payload, {"doi": doi}, record)
            authors = self._dspace_creators(payload)
            publications.append(
                {
                    "id": record.work_id or record.id,
                    "title": title,
                    "faculty": faculty_label,
                    "department": department_label,
                    "doi": doi,
                    "publication_year": record.publication_year,
                    "journal": None,
                    "document_type": self._source_record_document_type(payload) or "Article",
                    "citation_count": record.citation_count or 0,
                    "authors": authors,
                    "author_count": len(authors),
                    "source_names": ["dspace"],
                    "open_access_status": "repository",
                    "uri": publication_url,
                    "publication_url": publication_url,
                }
            )
            if len(publications) >= cache_key:
                break

        self._recent_publications_cache[cache_key] = (
            now + self._dashboard_cache_ttl_seconds,
            publications,
        )
        return publications

    def publications_browse(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        faculty: str | None = None,
        department: str | None = None,
        publication_years: list[int] | tuple[int, ...] | None = None,
        alphabet: str | None = None,
        search: str | None = None,
        article_only: bool = True,
    ) -> dict:
        page = max(int(page or 1), 1)
        page_size = min(max(int(page_size or 20), 1), 100)
        selected_years = set(self._normalize_publication_years(publication_years))
        selected_letter = str(alphabet or "").strip().lower()[:1]
        if selected_letter and not selected_letter.isalpha():
            selected_letter = ""
        search_query = self._normalize_search_text(search)

        if article_only and not selected_years:
            return self._publication_browse_metadata_page(
                page=page,
                page_size=page_size,
                faculty=faculty,
                department=department,
                selected_letter=selected_letter,
                search_query=search_query,
            )

        if (
            (faculty is None or faculty == "All faculties")
            and (department is None or department == "All departments")
        ):
            return self._publication_browse_sql_page(
                page=page,
                page_size=page_size,
                selected_years=selected_years,
                selected_letter=selected_letter,
                search_query=search_query,
                article_only=article_only,
            )

        rows = self._publication_browse_rows(
            faculty=faculty,
            department=department,
            selected_years=selected_years,
            selected_letter=selected_letter,
            search_query=search_query,
            article_only=article_only,
        )
        total = len(rows)
        start = (page - 1) * page_size
        end = start + page_size
        total_pages = max((total + page_size - 1) // page_size, 1)
        return {
            "rows": rows[start:end],
            "total": total,
            "page": min(page, total_pages),
            "page_size": page_size,
            "total_pages": total_pages,
        }

    def _publication_browse_metadata_page(
        self,
        *,
        page: int,
        page_size: int,
        faculty: str | None,
        department: str | None,
        selected_letter: str,
        search_query: str,
    ) -> dict:
        faculty_key = faculty or "All faculties"
        department_key = department or "All departments"
        cache_key = (faculty_key, department_key, selected_letter, search_query)
        now = time.monotonic()
        cached = self._publication_browse_metadata_rows_cache.get(cache_key)
        if cached is not None and now < cached[0]:
            rows = cached[1]
        else:
            rows = self._publication_browse_metadata_rows(
                faculty=faculty,
                department=department,
                selected_letter=selected_letter,
                search_query=search_query,
            )
            self._publication_browse_metadata_rows_cache[cache_key] = (
                now + self._dashboard_cache_ttl_seconds,
                rows,
            )

        total = len(rows)
        start = (page - 1) * page_size
        total_pages = max((total + page_size - 1) // page_size, 1)
        return {
            "rows": rows[start : start + page_size],
            "total": total,
            "page": min(page, total_pages),
            "page_size": page_size,
            "total_pages": total_pages,
        }

    def _publication_browse_metadata_rows(
        self,
        *,
        faculty: str | None,
        department: str | None,
        selected_letter: str,
        search_query: str,
    ) -> list[dict]:
        rows: list[dict] = []
        for row in self._publication_browse_base_rows():
            record_faculty = row.get("faculty") or "Unmapped"
            record_department = row.get("department") or "Unmapped"
            if faculty and faculty != "All faculties" and record_faculty != faculty:
                continue
            if department and department != "All departments" and record_department != department:
                continue
            title = (row.get("title") or "").strip()
            if not title or title == "Untitled publication":
                continue
            normalized_title = self._normalize_search_text(title)
            if search_query and search_query not in normalized_title:
                continue
            if selected_letter and not normalized_title.startswith(selected_letter):
                continue
            rows.append(row)

        rows.sort(
            key=lambda row: (
                str(row.get("title") or "").lower()
                if selected_letter
                else -(row.get("publication_year") or 0),
                str(row.get("title") or "").lower(),
            )
        )
        return rows

    def _publication_browse_base_rows(self) -> list[dict]:
        now = time.monotonic()
        cached = self._publication_browse_base_rows_cache
        if cached is not None and now < cached[0]:
            return cached[1]

        snapshot_rows = self._load_publication_browse_cache_snapshot()
        if snapshot_rows is not None:
            self._publication_browse_base_rows_cache = (
                now + self._dashboard_cache_ttl_seconds,
                snapshot_rows,
            )
            return snapshot_rows

        rows: list[dict] = []
        for index, record in enumerate(self._article_metadata_records()):
            if not self._metadata_record_is_article(record):
                continue
            title = (record.get("Article Title") or "").strip()
            if not title or title == "Untitled publication":
                continue
            doi = (record.get("DOI") or "").strip()
            uri = self._first_url([record.get("Article URI")])
            authors = self._iter_author_entries([record.get("Main Author") or ""])
            rows.append(
                {
                    "id": f"metadata-{index}",
                    "title": title,
                    "faculty": self._visible_mapping_label(record.get("Faculty")) or "Unmapped",
                    "department": self._visible_mapping_label(record.get("Department")) or "Unmapped",
                    "doi": doi or None,
                    "publication_year": self._metadata_publication_year(record),
                    "journal": None,
                    "document_type": "Article",
                    "citation_count": 0,
                    "authors": authors,
                    "author_count": len(authors),
                    "source_names": ["article metadata"],
                    "open_access_status": "Unknown",
                    "uri": uri or (f"https://doi.org/{doi}" if doi else None),
                    "publication_url": uri or (f"https://doi.org/{doi}" if doi else None),
                }
            )
        rows.sort(
            key=lambda row: (
                -(row.get("publication_year") or 0),
                str(row.get("title") or "").lower(),
            )
        )
        self._save_publication_browse_cache_snapshot(rows)
        self._publication_browse_base_rows_cache = (
            now + self._dashboard_cache_ttl_seconds,
            rows,
        )
        return rows

    def _load_publication_browse_cache_snapshot(self) -> list[dict] | None:
        try:
            with self._publication_browse_cache_file.open("r", encoding="utf-8") as handle:
                snapshot = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("version") != self._publication_browse_cache_snapshot_version
        ):
            return None
        metadata_mtime = snapshot.get("article_metadata_mtime")
        if metadata_mtime != self._article_metadata_file_mtime():
            return None
        rows = snapshot.get("rows")
        return rows if isinstance(rows, list) else None

    def _save_publication_browse_cache_snapshot(self, rows: list[dict]) -> None:
        try:
            self._publication_browse_cache_file.parent.mkdir(parents=True, exist_ok=True)
            with self._publication_browse_cache_file.open("w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "version": self._publication_browse_cache_snapshot_version,
                        "article_metadata_mtime": self._article_metadata_file_mtime(),
                        "rows": rows,
                    },
                    handle,
                    ensure_ascii=False,
                )
        except OSError:
            logger.exception("Unable to save publication browse cache snapshot")

    def _article_metadata_file_mtime(self) -> float | None:
        try:
            return self._article_metadata_file.stat().st_mtime
        except OSError:
            return None

    def _publication_browse_sql_page(
        self,
        *,
        page: int,
        page_size: int,
        selected_years: set[int],
        selected_letter: str,
        search_query: str,
        article_only: bool,
    ) -> dict:
        sort_year = func.coalesce(SourceRecord.publication_year, Work.publication_year)
        base_filters = [SourceRecord.source_name == "dspace"]
        if article_only:
            base_filters.append(
                Work.document_type.ilike("%article%")
                | SourceRecord.raw_payload["type"].as_string().ilike("%article%")
                | SourceRecord.raw_payload["subtype"].as_string().ilike("%article%")
                | SourceRecord.raw_payload["subtypeDescription"].as_string().ilike("%article%")
                | SourceRecord.raw_payload["dc:type"].as_string().ilike("%article%")
            )
        if selected_years:
            base_filters.append(sort_year.in_(selected_years))
        if search_query:
            like_query = f"%{search_query}%"
            base_filters.append(
                cast(Work.title, String).ilike(like_query)
                | cast(SourceRecord.title, String).ilike(like_query)
            )
        if selected_letter:
            like_letter = f"{selected_letter}%"
            base_filters.append(
                cast(Work.title, String).ilike(like_letter)
                | cast(SourceRecord.title, String).ilike(like_letter)
            )

        offset = (page - 1) * page_size
        session = SessionLocal()
        try:
            total = int(
                session.execute(
                    select(func.count()).select_from(SourceRecord).join(Work, SourceRecord.work_id == Work.id).where(*base_filters)
                ).scalar()
                or 0
            )
            rows = session.execute(
                select(Work, SourceRecord)
                .join(SourceRecord, SourceRecord.work_id == Work.id)
                .where(*base_filters)
                .order_by(sort_year.desc(), SourceRecord.id.desc())
                .offset(offset)
                .limit(page_size)
            ).all()
        finally:
            session.close()

        publications = [
            self._publication_browse_row_from_source_record(work, source_record)
            for work, source_record in rows
        ]
        total_pages = max((total + page_size - 1) // page_size, 1)
        return {
            "rows": publications,
            "total": total,
            "page": min(page, total_pages),
            "page_size": page_size,
            "total_pages": total_pages,
        }

    def _publication_browse_rows(
        self,
        *,
        faculty: str | None,
        department: str | None,
        selected_years: set[int],
        selected_letter: str,
        search_query: str,
        article_only: bool,
    ) -> list[dict]:
        sort_year = func.coalesce(SourceRecord.publication_year, Work.publication_year)
        query = (
            select(Work, SourceRecord)
            .join(SourceRecord, SourceRecord.work_id == Work.id)
            .where(SourceRecord.source_name == "dspace")
            .order_by(sort_year.desc(), SourceRecord.id.desc())
        )
        if article_only:
            query = query.where(
                Work.document_type.ilike("%article%")
                | SourceRecord.raw_payload["type"].as_string().ilike("%article%")
                | SourceRecord.raw_payload["subtype"].as_string().ilike("%article%")
                | SourceRecord.raw_payload["subtypeDescription"].as_string().ilike("%article%")
                | SourceRecord.raw_payload["dc:type"].as_string().ilike("%article%")
            )
        if selected_years:
            query = query.where(sort_year.in_(selected_years))
        if search_query:
            like_query = f"%{search_query}%"
            query = query.where(
                cast(Work.title, String).ilike(like_query)
                | cast(SourceRecord.title, String).ilike(like_query)
            )
        if selected_letter:
            like_letter = f"{selected_letter}%"
            query = query.where(
                cast(Work.title, String).ilike(like_letter)
                | cast(SourceRecord.title, String).ilike(like_letter)
            )

        session = SessionLocal()
        try:
            source_rows = session.execute(query).all()
        finally:
            session.close()

        publications: list[dict] = []
        seen_keys: set[str] = set()
        for work, source_record in source_rows:
            payload = source_record.raw_payload or {}
            title = source_record.title or self._source_record_title(payload) or work.title
            if not title:
                continue
            title_key = self._title_fingerprint(title)
            normalized_title = self._normalize_search_text(title)
            if search_query and search_query not in normalized_title:
                continue
            if selected_letter and not normalized_title.startswith(selected_letter):
                continue

            faculty_value, department_value = self._dspace_faculty_department(
                dspace_payload=payload,
                title=title,
                journal=work.journal,
                publisher=work.publisher,
                keywords=work.keywords or [],
            )
            faculty_label = self._visible_mapping_label(faculty_value) or "Unmapped"
            department_label = self._visible_mapping_label(department_value) or "Unmapped"
            if faculty and faculty != "All faculties" and faculty_label != faculty:
                continue
            if department and department != "All departments" and department_label != department:
                continue

            doi_key = str(work.doi or source_record.doi or "").strip().lower()
            row_keys = [
                key
                for key in (
                    f"doi:{doi_key}" if doi_key else "",
                    f"title:{title_key}" if title_key else "",
                )
                if key
            ] or [str(work.id or source_record.id)]
            if any(key in seen_keys for key in row_keys):
                continue
            seen_keys.update(row_keys)

            publication_url = self._source_record_url(source_record, work, payload)
            publications.append(
                self._publication_browse_row_from_source_record(
                    work,
                    source_record,
                    title=title,
                    faculty_label=faculty_label,
                    department_label=department_label,
                    publication_url=publication_url,
                )
            )
        return publications

    def _publication_browse_row_from_source_record(
        self,
        work: Work,
        source_record: SourceRecord,
        *,
        title: str | None = None,
        faculty_label: str | None = None,
        department_label: str | None = None,
        publication_url: str | None = None,
    ) -> dict:
        payload = source_record.raw_payload or {}
        title = title or source_record.title or self._source_record_title(payload) or work.title or "Untitled publication"
        if faculty_label is None or department_label is None:
            faculty_value, department_value = self._dspace_faculty_department(
                dspace_payload=payload,
                title=title,
                journal=work.journal,
                publisher=work.publisher,
                keywords=work.keywords or [],
            )
            faculty_label = self._visible_mapping_label(faculty_value) or "Unmapped"
            department_label = self._visible_mapping_label(department_value) or "Unmapped"
        publication_url = publication_url or self._source_record_url(source_record, work, payload)
        authors = (
            self._dspace_creators(payload)
            if source_record.source_name == "dspace"
            else self._iter_author_entries(work.authors or [])
        )
        if not authors:
            authors = self._iter_author_entries(work.authors or [])
        return {
            "id": work.id or source_record.id,
            "title": title,
            "faculty": faculty_label,
            "department": department_label,
            "doi": work.doi or source_record.doi,
            "publication_year": source_record.publication_year or work.publication_year,
            "journal": work.journal,
            "document_type": work.document_type or "Article",
            "citation_count": work.citation_count or source_record.citation_count,
            "authors": authors,
            "author_count": len(authors),
            "source_names": work.source_names or [source_record.source_name],
            "open_access_status": work.open_access_status,
            "uri": publication_url,
            "publication_url": publication_url,
        }

    def _recent_publications_fast(self, limit: int) -> list[dict]:
        now = time.monotonic()
        cached = self._recent_publications_cache.get(limit)
        if cached is not None and now < cached[0]:
            return cached[1]

        fetch_limit = max(limit * 10, 500)
        session = SessionLocal()
        try:
            rows = session.execute(
                select(Work, SourceRecord)
                .join(SourceRecord, SourceRecord.work_id == Work.id)
                .where(SourceRecord.source_name == "dspace")
                .where(Work.document_type.ilike("%article%"))
                .order_by(Work.publication_year.desc(), Work.id.desc())
                .limit(fetch_limit)
            ).all()
        finally:
            session.close()

        publications: list[dict] = []
        seen_work_ids: set[int] = set()
        for work, dspace_record in rows:
            if work.id in seen_work_ids:
                continue
            seen_work_ids.add(work.id)

            dspace_payload = dspace_record.raw_payload or {}
            dspace_title = dspace_record.title or self._first_dspace_value(dspace_payload, "title") or work.title
            dspace_authors = self._dspace_creators(dspace_payload)
            publication_url = self._dspace_record_url(dspace_payload, work, dspace_record)
            faculty, department = self._dspace_faculty_department(
                dspace_payload=dspace_payload,
                title=dspace_title,
                journal=work.journal,
                publisher=work.publisher,
                keywords=work.keywords or [],
            )
            faculty_label = self._visible_mapping_label(faculty)
            department_label = self._visible_mapping_label(department)
            if not faculty_label or not department_label:
                continue

            publications.append(
                {
                    "id": work.id,
                    "title": dspace_title,
                    "faculty": faculty_label,
                    "department": department_label,
                    "doi": work.doi or dspace_record.doi,
                    "publication_year": work.publication_year or dspace_record.publication_year,
                    "journal": work.journal,
                    "document_type": work.document_type,
                    "citation_count": work.citation_count or dspace_record.citation_count,
                    "authors": dspace_authors or self._iter_author_entries(work.authors or []),
                    "author_count": len(dspace_authors or work.authors or []),
                    "source_names": work.source_names or ["dspace"],
                    "open_access_status": work.open_access_status,
                    "uri": publication_url,
                    "publication_url": publication_url,
                }
            )
            if len(publications) >= limit:
                break

        if not publications:
            publications = self._recent_publications_from_works_fast(limit)

        self._recent_publications_cache[limit] = (
            now + self._dashboard_cache_ttl_seconds,
            publications,
        )
        return publications

    def _recent_publications_from_works_fast(self, limit: int, article_only: bool = True) -> list[dict]:
        fetch_limit = max(limit * 10, 500)
        query = select(Work).order_by(Work.publication_year.desc(), Work.id.desc()).limit(fetch_limit)
        if article_only:
            query = (
                select(Work)
                .where(Work.document_type.ilike("%article%"))
                .order_by(Work.publication_year.desc(), Work.id.desc())
                .limit(fetch_limit)
            )
        session = SessionLocal()
        try:
            works = session.execute(query).scalars().all()
        finally:
            session.close()

        publications: list[dict] = []
        for work in works:
            faculty, department = classify_work(work)
            faculty_label = self._visible_mapping_label(faculty) or "Not classified"
            department_label = self._visible_mapping_label(department) or "Not classified"

            publication_url = self._publication_url(work)
            authors = self._iter_author_entries(work.authors or [])
            publications.append(
                {
                    "id": work.id,
                    "title": work.title,
                    "faculty": faculty_label,
                    "department": department_label,
                    "doi": work.doi,
                    "publication_year": work.publication_year,
                    "journal": work.journal,
                    "document_type": work.document_type,
                    "citation_count": work.citation_count,
                    "authors": authors,
                    "author_count": len(authors),
                    "source_names": work.source_names or [],
                    "open_access_status": work.open_access_status,
                    "uri": publication_url,
                    "publication_url": publication_url,
                }
            )
            if len(publications) >= limit:
                break
        return publications

    def _recent_publications_from_source_records_fast(
        self,
        *,
        limit: int,
        faculty: str | None = None,
        department: str | None = None,
        publication_years: list[int] | tuple[int, ...] | None = None,
        article_only: bool = True,
    ) -> list[dict]:
        selected_years = set(self._normalize_publication_years(publication_years))
        fetch_limit = max(limit * 10, 500)
        sort_year = func.coalesce(SourceRecord.publication_year, Work.publication_year)
        query = (
            select(Work, SourceRecord)
            .join(SourceRecord, SourceRecord.work_id == Work.id)
            .order_by(sort_year.desc(), SourceRecord.id.desc())
            .limit(fetch_limit)
        )
        if article_only:
            query = query.where(
                Work.document_type.ilike("%article%")
                | SourceRecord.raw_payload["type"].as_string().ilike("%article%")
                | SourceRecord.raw_payload["subtype"].as_string().ilike("%article%")
                | SourceRecord.raw_payload["subtypeDescription"].as_string().ilike("%article%")
                | SourceRecord.raw_payload["dc:type"].as_string().ilike("%article%")
            )
        if selected_years:
            query = query.where(sort_year.in_(selected_years))

        session = SessionLocal()
        try:
            rows = session.execute(query).all()
        finally:
            session.close()

        publications: list[dict] = []
        seen_keys: set[str] = set()
        for work, source_record in rows:
            payload = source_record.raw_payload or {}
            title = source_record.title or self._source_record_title(payload) or work.title
            if not title:
                continue

            if source_record.source_name == "dspace":
                faculty_value, department_value = self._dspace_faculty_department(
                    dspace_payload=payload,
                    title=title,
                    journal=work.journal,
                    publisher=work.publisher,
                    keywords=work.keywords or [],
                )
            else:
                faculty_value, department_value = classify_work(work)
            faculty_label = self._visible_mapping_label(faculty_value) or "Not classified"
            department_label = self._visible_mapping_label(department_value) or "Not classified"
            if faculty and faculty != "All faculties" and faculty_label != faculty:
                continue
            if department and department != "All departments" and department_label != department:
                continue

            doi_key = str(work.doi or source_record.doi or "").strip().lower()
            title_key = self._title_fingerprint(title)
            row_keys = [key for key in (f"doi:{doi_key}" if doi_key else "", f"title:{title_key}" if title_key else "") if key]
            if not row_keys:
                row_keys = [str(work.id or source_record.id)]
            if any(key in seen_keys for key in row_keys):
                continue
            seen_keys.update(row_keys)

            doi = work.doi or source_record.doi
            publication_url = self._source_record_url(source_record, work, payload)
            authors = (
                self._dspace_creators(payload)
                if source_record.source_name == "dspace"
                else self._iter_author_entries(work.authors or [])
            )
            if not authors:
                authors = self._iter_author_entries(work.authors or [])
            publications.append(
                {
                    "id": work.id or source_record.id,
                    "title": title,
                    "faculty": faculty_label,
                    "department": department_label,
                    "doi": doi,
                    "publication_year": source_record.publication_year or work.publication_year,
                    "journal": work.journal,
                    "document_type": work.document_type or "Article",
                    "citation_count": work.citation_count or source_record.citation_count,
                    "authors": authors,
                    "author_count": len(authors),
                    "source_names": work.source_names or [source_record.source_name],
                    "open_access_status": work.open_access_status,
                    "uri": publication_url,
                    "publication_url": publication_url,
                }
            )
            if len(publications) >= limit:
                break
        return publications

    def _source_record_title(self, payload: dict) -> str | None:
        if not isinstance(payload, dict):
            return None
        for key in ("display_name", "title", "dc:title"):
            value = payload.get(key)
            if isinstance(value, list):
                value = value[0] if value else None
            if value:
                return " ".join(str(value).split())
        return None

    def _source_record_doi(self, payload: dict) -> str | None:
        if not isinstance(payload, dict):
            return None
        for key in ("doi", "dc:identifier.doi", "identifier"):
            values = payload.get(key)
            if not isinstance(values, list):
                values = [values]
            for value in values:
                text = " ".join(str(value or "").split()).strip()
                match = re.search(r"\b10\.\d{4,9}/[^\s,\];)]+", text, flags=re.IGNORECASE)
                if match:
                    return match.group(0).rstrip(".")
        return None

    def _source_record_url(self, source_record: SourceRecord, work: Work | dict, payload: dict) -> str | None:
        if source_record.source_name == "dspace" and isinstance(payload, dict):
            return self._dspace_record_url(payload, work, source_record)
        return self._publication_url(
            {
                "primary_source_url": self._work_value(work, "primary_source_url") or source_record.source_url,
                "doi": self._work_value(work, "doi") or source_record.doi,
            }
        )

    def _normalize_doi_key(self, value: str | None) -> str:
        text = " ".join(str(value or "").split()).strip().lower()
        if not text:
            return ""
        text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text)
        text = re.sub(r"^doi:\s*", "", text)
        return text.strip(" .")

    def _normalize_url_key(self, value: str | None) -> str:
        text = " ".join(str(value or "").split()).strip().lower()
        return text.rstrip(".,;") if text else ""

    def _source_record_document_type(self, payload: dict) -> str | None:
        if not isinstance(payload, dict):
            return None
        for key in ("type", "subtypeDescription", "subtype", "dc:type", "documentType"):
            value = payload.get(key)
            if isinstance(value, list):
                value = value[0] if value else None
            if value:
                return " ".join(str(value).split())
        return None

    def _source_record_citation_count(self, payload: dict) -> int | None:
        if not isinstance(payload, dict):
            return None
        for key in ("cited_by_count", "citation_count", "citedByCount"):
            value = payload.get(key)
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return None

    def _source_record_open_access_status(self, payload: dict) -> str | None:
        if not isinstance(payload, dict):
            return None
        open_access = payload.get("open_access")
        if isinstance(open_access, dict) and open_access.get("oa_status"):
            return str(open_access.get("oa_status"))
        for key in ("open_access_status", "oa_status"):
            if payload.get(key):
                return str(payload.get(key))
        return None

    def _source_record_keywords(self, payload: dict) -> list[str]:
        if not isinstance(payload, dict):
            return []
        keywords: list[str] = []
        for key in ("keywords", "subject"):
            value = payload.get(key)
            values = value if isinstance(value, list) else [value] if value else []
            for item in values:
                label = " ".join(str(item or "").split()).strip()
                if label:
                    keywords.append(label)
        for concept in payload.get("concepts") or []:
            if not isinstance(concept, dict):
                continue
            label = " ".join(str(concept.get("display_name") or "").split()).strip()
            if label:
                keywords.append(label)
        seen: set[str] = set()
        deduped: list[str] = []
        for keyword in keywords:
            key = keyword.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(keyword)
        return deduped

    def _recent_publications_from_metadata_export(
        self,
        *,
        limit: int,
        faculty: str | None = None,
        department: str | None = None,
        publication_years: list[int] | tuple[int, ...] | None = None,
    ) -> list[dict]:
        selected_years = set(self._normalize_publication_years(publication_years))

        publications: list[dict] = []
        for index, record in enumerate(self._article_metadata_records()):
            record_faculty = self._visible_mapping_label(record.get("Faculty")) or "Not classified"
            record_department = self._visible_mapping_label(record.get("Department")) or "Not classified"
            if faculty and faculty != "All faculties" and record_faculty != faculty:
                continue
            if department and department != "All departments" and record_department != department:
                continue
            publication_year = self._metadata_publication_year(record)
            if selected_years and publication_year not in selected_years:
                continue

            title = (record.get("Article Title") or "").strip()
            if not title or title == "Untitled publication":
                continue

            doi = (record.get("DOI") or "").strip()
            uri = self._first_url([record.get("Article URI")])
            authors = self._iter_author_entries([record.get("Main Author") or ""])
            publications.append(
                {
                    "id": f"metadata-{index}",
                    "title": title,
                    "faculty": record_faculty,
                    "department": record_department,
                    "doi": doi or None,
                    "publication_year": publication_year,
                    "journal": None,
                    "document_type": "Article",
                    "citation_count": 0,
                    "authors": authors,
                    "author_count": len(authors),
                    "source_names": ["article metadata"],
                    "open_access_status": "Unknown",
                    "uri": uri or (f"https://doi.org/{doi}" if doi else None),
                    "publication_url": uri or (f"https://doi.org/{doi}" if doi else None),
                }
            )
            if len(publications) >= limit:
                break
        return publications

    def articles_by_document_type(
        self,
        faculty: str | None = None,
        department: str | None = None,
        publication_years: list[int] | tuple[int, ...] | None = None,
    ) -> list[dict]:
        rows = self._filter_annotated_works(
            self._dashboard_rows(),
            faculty,
            department,
            False,
            publication_years,
        )
        rows = self._cluster_dashboard_rows(rows)

        grouped: dict[str, dict] = {}
        for item in rows:
            work = item["work"]
            document_type = self._work_value(work, "document_type") or "unknown"
            authors = self._work_list(work, "authors")
            institutions = self._work_list(work, "openalex_institutions") or self._work_list(work, "affiliations")
            main_institution, affiliate_institutions = self._main_and_affiliate_institutions(institutions)
            article = {
                "id": self._work_value(work, "id"),
                "title": self._work_value(work, "title"),
                "main_author": self._work_value(work, "main_author") or (authors[0] if authors else None),
                "doi": self._work_value(work, "doi"),
                "faculty": item["faculty"] or "Unmapped",
                "department": item["department"] or "Unmapped",
                "main institution": main_institution,
                "affiliate institutions": "; ".join(affiliate_institutions) if affiliate_institutions else None,
                "institution": main_institution,
                "institutions": institutions,
                "publication_year": self._work_value(work, "publication_year"),
                "source_names": "; ".join(self._work_list(work, "source_names")),
            }
            group = grouped.setdefault(
                document_type,
                {
                    "document_type": document_type,
                    "count": 0,
                    "articles": [],
                },
            )
            group["articles"].append(article)
            group["count"] += 1

        for group in grouped.values():
            group["articles"].sort(
                key=lambda article: (
                    article["title"] or "",
                    article["main_author"] or "",
                    article["doi"] or "",
                )
            )

        return sorted(grouped.values(), key=lambda group: (group["document_type"] or "").lower())

    def article_metadata_rows(
        self,
        faculty: str | None = None,
        department: str | None = None,
        publication_years: list[int] | tuple[int, ...] | None = None,
    ) -> list[dict]:
        rows: list[dict] = []
        for group in self.articles_by_document_type(faculty, department, publication_years):
            for article in group["articles"]:
                rows.append(
                    {
                        "title": article.get("title"),
                        "main_author": article.get("main_author"),
                        "doi": article.get("doi"),
                        "faculty": article.get("faculty"),
                        "department": article.get("department"),
                        "main institution": article.get("main institution"),
                        "affiliate institutions": article.get("affiliate institutions"),
                        "publication_year": article.get("publication_year"),
                        "source_names": article.get("source_names"),
                    }
                )
        return rows

    def recent_works(
        self,
        limit: int = 25,
        faculty: str | None = None,
        department: str | None = None,
        publication_years: list[int] | tuple[int, ...] | None = None,
        article_only: bool = False,
    ) -> list[dict]:
        return self.publications(
            limit=limit,
            faculty=faculty,
            department=department,
            publication_years=publication_years,
            article_only=article_only,
        )

    def recent_runs(self, limit: int = 10) -> list[dict]:
        session = SessionLocal()
        try:
            rows = session.execute(
                select(HarvestRun).order_by(HarvestRun.started_at.desc()).limit(limit)
            ).scalars()
            return [self._serialize_run(run) for run in rows]
        finally:
            session.close()

    def _load_dashboard_cache_snapshot(self) -> None:
        try:
            with self._dashboard_cache_file.open("r", encoding="utf-8") as handle:
                snapshot = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return

        now = time.monotonic()
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("version") != self._dashboard_cache_snapshot_version
            or snapshot.get("db_fingerprint") != self._source_records_cache_fingerprint()
        ):
            return

        entries = snapshot.get("entries", [])
        for entry in entries:
            try:
                cache_key = self._deserialize_dashboard_cache_key(entry["key"])
                payload = entry["payload"]
            except (KeyError, TypeError, ValueError):
                continue
            if isinstance(payload, dict):
                if cache_key == (None, None, (), False):
                    overview = payload.get("overview")
                    if not isinstance(overview, dict):
                        continue
                    if overview.get("metric_computation_version") != self._dashboard_metric_computation_version:
                        continue
                self._dashboard_cache[cache_key] = (now + self._dashboard_cache_ttl_seconds, payload)

    def _save_dashboard_cache_snapshot(self) -> None:
        entries = [
            {
                "key": self._serialize_dashboard_cache_key(cache_key),
                "payload": payload,
            }
            for cache_key, (_, payload) in self._dashboard_cache.items()
        ]
        try:
            self._dashboard_cache_file.parent.mkdir(parents=True, exist_ok=True)
            with self._dashboard_cache_file.open("w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "version": self._dashboard_cache_snapshot_version,
                        "db_fingerprint": self._source_records_cache_fingerprint(),
                        "entries": entries,
                    },
                    handle,
                )
        except OSError:
            return

    def _serialize_dashboard_cache_key(
        self,
        cache_key: tuple[str | None, str | None, tuple[int, ...], bool],
    ) -> list:
        faculty, department, publication_years, article_only = cache_key
        return [faculty, department, list(publication_years), article_only]

    def _deserialize_dashboard_cache_key(
        self,
        value: list,
    ) -> tuple[str | None, str | None, tuple[int, ...], bool]:
        if not isinstance(value, list) or len(value) != 4:
            raise ValueError("Invalid dashboard cache key")
        faculty, department, publication_years, article_only = value
        return (
            faculty,
            department,
            tuple(int(year) for year in publication_years),
            bool(article_only),
        )

    def _load_researchers_cache_snapshot(self) -> None:
        try:
            with self._researchers_cache_file.open("r", encoding="utf-8") as handle:
                header = handle.read(80)
            compact_header = "".join(header.split())
            if f'"version":{self._researchers_cache_snapshot_version}' not in compact_header:
                return
            with self._researchers_cache_file.open("r", encoding="utf-8") as handle:
                snapshot = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return

        if (
            not isinstance(snapshot, dict)
            or snapshot.get("version") != self._researchers_cache_snapshot_version
            or snapshot.get("db_fingerprint") != self._source_records_cache_fingerprint()
        ):
            return

        researchers = snapshot.get("researchers")
        if not isinstance(researchers, list):
            return

        self._researchers_all_works_cache = (
            time.monotonic() + self._researchers_cache_ttl_seconds,
            researchers,
        )
        self._researchers_all_works_cache_source = "FAST"
        self._researchers_by_author_key = self._index_researcher_summaries_by_author_key(researchers)
        self._last_researchers_cache_status = "FAST"

    def _save_researchers_cache_snapshot(self, researchers: list[dict]) -> None:
        try:
            self._researchers_cache_file.parent.mkdir(parents=True, exist_ok=True)
            with self._researchers_cache_file.open("w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "version": self._researchers_cache_snapshot_version,
                        "db_fingerprint": self._source_records_cache_fingerprint(),
                        "researchers": researchers,
                    },
                    handle,
                )
        except OSError:
            return

    def _load_dashboard_rows_cache_snapshot(self) -> list[dict] | None:
        try:
            with self._dashboard_rows_cache_file.open("r", encoding="utf-8") as handle:
                snapshot = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None

        if (
            not isinstance(snapshot, dict)
            or snapshot.get("version") != self._dashboard_rows_cache_snapshot_version
            or snapshot.get("db_fingerprint") != self._source_records_cache_fingerprint()
        ):
            return None

        rows = snapshot.get("rows")
        if not isinstance(rows, list):
            return None

        self._dashboard_rows_cache = rows
        self._dashboard_rows_cache_expires_at = time.monotonic() + self._dashboard_cache_ttl_seconds
        return rows

    def _save_dashboard_rows_cache_snapshot(self, rows: list[dict]) -> None:
        try:
            self._dashboard_rows_cache_file.parent.mkdir(parents=True, exist_ok=True)
            with self._dashboard_rows_cache_file.open("w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "version": self._dashboard_rows_cache_snapshot_version,
                        "db_fingerprint": self._source_records_cache_fingerprint(),
                        "rows": rows,
                    },
                    handle,
                )
        except OSError:
            return

    def _source_records_cache_fingerprint(self) -> dict:
        session = SessionLocal()
        try:
            row = session.execute(
                select(
                    func.count(SourceRecord.id),
                    func.max(SourceRecord.id),
                    func.max(SourceRecord.harvested_at),
                )
            ).one()
        except Exception:
            return {}
        finally:
            session.close()

        count, max_id, harvested_at = row
        return {
            "source_record_count": int(count or 0),
            "max_source_record_id": int(max_id or 0),
            "max_harvested_at": harvested_at.isoformat() if harvested_at else None,
        }

    def clear_cache(self) -> None:
        self._works_cache = None
        self._works_cache_expires_at = 0.0
        self._annotated_cache = None
        self._annotated_cache_expires_at = 0.0
        self._dashboard_rows_cache = None
        self._dashboard_rows_cache_expires_at = 0.0
        self._dashboard_cache = {}
        self._dspace_baseline_cache = None
        self._researchers_all_works_cache = None
        self._researchers_by_author_key = {}
        self._researcher_rows_cache = None
        self._researcher_rows_by_author_key = {}
        self._recent_publications_cache = {}
        self._publication_browse_metadata_rows_cache = {}
        self._publication_browse_base_rows_cache = None
        self._researcher_profile_cache = {}
        self._openalex_institution_author_count_cache = None
        self._openalex_author_metrics_cache = {}
        self._researcher_orcid_cache = {}
        self._work_source_title_match_cache = {}
        self._dashboard_metric_cache = {}
        self._last_researchers_cache_status = "BYPASS"
        try:
            self._dashboard_cache_file.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        try:
            self._dashboard_rows_cache_file.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        try:
            self._researchers_cache_file.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        try:
            self._publication_browse_cache_file.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def warm_dashboard_cache(self) -> None:
        self._load_dashboard_cache_snapshot()
        self.dashboard_bundle()
        self._load_researchers_cache_snapshot()
        self._refresh_researcher_rows_cache_async()

    def _all_works(self) -> list[Work]:
        now = time.monotonic()
        if self._works_cache is not None and now < self._works_cache_expires_at:
            return self._works_cache

        session = SessionLocal()
        try:
            works = list(session.execute(select(Work)).scalars())
        finally:
            session.close()

        self._works_cache = works
        self._works_cache_expires_at = now + self._works_cache_ttl_seconds
        return works

    def _annotated_works(self) -> list[dict]:
        now = time.monotonic()
        if self._annotated_cache is not None and now < self._annotated_cache_expires_at:
            return self._annotated_cache

        annotated: list[dict] = []
        for work in self._all_works():
            faculty, department = classify_work(work)
            annotated.append(
                {
                    "work": work,
                    "faculty": faculty,
                    "department": department,
                }
            )

        self._annotated_cache = annotated
        self._annotated_cache_expires_at = now + self._dashboard_cache_ttl_seconds
        return annotated

    def _dashboard_rows(self) -> list[dict]:
        now = time.monotonic()
        if self._dashboard_rows_cache is not None and now < self._dashboard_rows_cache_expires_at:
            return self._dashboard_rows_cache

        session = SessionLocal()
        try:
            dspace_rows = session.execute(
                select(Work, SourceRecord)
                .join(SourceRecord, SourceRecord.work_id == Work.id)
                .where(SourceRecord.source_name == "dspace")
            ).all()
            openalex_institutions_by_work_id = self._openalex_institutions_by_work_id(
                session,
                [work.id for work, _ in dspace_rows],
            )
        finally:
            session.close()

        dashboard_rows: list[dict] = []
        if dspace_rows:
            main_author_home_by_key = self._main_author_home_by_key(dspace_rows)
            for work, dspace_record in dspace_rows:
                dspace_payload = dspace_record.raw_payload or {}
                dspace_title = dspace_record.title or self._first_dspace_value(dspace_payload, "title") or work.title
                dspace_authors = self._dspace_creators(dspace_payload)
                main_author = dspace_authors[0] if dspace_authors else None
                publication_url = self._dspace_record_url(dspace_payload, work, dspace_record)
                faculty, department = self._main_author_faculty_department(
                    main_author=main_author,
                    main_author_home_by_key=main_author_home_by_key,
                    dspace_payload=dspace_payload,
                    title=dspace_title,
                    journal=work.journal,
                    publisher=work.publisher,
                    keywords=work.keywords or [],
                )
                openalex_institutions = openalex_institutions_by_work_id.get(work.id, [])

                dashboard_rows.append(
                    {
                        "work": {
                            "id": work.id,
                            "source_record_id": dspace_record.source_record_id,
                            "title": dspace_title,
                            "doi": work.doi or dspace_record.doi,
                            "journal": work.journal,
                            "publisher": work.publisher,
                            "authors": [main_author] if main_author else [],
                            "all_dspace_authors": dspace_authors,
                            "main_author": main_author,
                            "affiliations": openalex_institutions or work.affiliations or [],
                            "keywords": work.keywords or self._dspace_values(dspace_payload, "subject"),
                            "publication_year": work.publication_year or dspace_record.publication_year,
                            "document_type": work.document_type,
                            "open_access_status": work.open_access_status,
                            "citation_count": work.citation_count or dspace_record.citation_count,
                            "primary_source_url": publication_url,
                            "uri": publication_url,
                            "source_count": work.source_count,
                            "source_names": work.source_names or ["dspace"],
                            "openalex_institutions": openalex_institutions,
                        },
                        "faculty": faculty,
                        "department": department,
                    }
                )

        works_dashboard_rows = self._dashboard_rows_from_works()
        if dashboard_rows and works_dashboard_rows:
            dspace_work_ids = {
                self._work_value(item.get("work") or {}, "id")
                for item in dashboard_rows
            }
            dashboard_rows.extend(
                item
                for item in works_dashboard_rows
                if self._work_value(item.get("work") or {}, "id") not in dspace_work_ids
            )
        elif works_dashboard_rows and (
            not dashboard_rows
            or self._dashboard_rows_quality_score(works_dashboard_rows)
            > self._dashboard_rows_quality_score(dashboard_rows)
        ):
            dashboard_rows = works_dashboard_rows

        self._dashboard_rows_cache = dashboard_rows
        self._dashboard_rows_cache_expires_at = now + self._dashboard_cache_ttl_seconds
        self._save_dashboard_rows_cache_snapshot(dashboard_rows)
        return dashboard_rows

    def _dashboard_rows_quality_score(self, rows: list[dict]) -> int:
        score = 0
        for item in rows:
            work = item.get("work") or {}
            if self._work_value(work, "publication_year") is not None:
                score += 4
            if self._work_value(work, "citation_count"):
                score += 4
            document_type = str(self._work_value(work, "document_type") or "").strip().lower()
            if document_type and document_type != "unknown":
                score += 3
            source_names = [
                str(source or "").strip().lower()
                for source in self._work_list(work, "source_names")
                if str(source or "").strip()
            ]
            if source_names:
                score += 1
            if any(source != "dspace" for source in source_names):
                score += 3
            if self._work_list(work, "keywords"):
                score += 2
        return score

    def _dashboard_rows_from_works(self) -> list[dict]:
        session = SessionLocal()
        try:
            works = list(session.execute(select(Work)).scalars())
            researcher_affiliations_by_work_id = self._researcher_affiliations_by_work_id(session)
        finally:
            session.close()

        dashboard_rows: list[dict] = []
        for work in works:
            researcher_affiliations = researcher_affiliations_by_work_id.get(work.id, [])
            affiliations = self._merge_text_lists(work.affiliations, researcher_affiliations)
            faculty, department = classify_record(
                title=work.title,
                abstract=work.abstract,
                journal=work.journal,
                publisher=work.publisher,
                authors=work.authors or [],
                affiliations=affiliations,
                keywords=work.keywords or [],
            )
            publication_url = self._publication_url(work)
            dashboard_rows.append(
                {
                    "work": {
                        "id": work.id,
                        "source_record_id": None,
                        "title": work.title,
                        "doi": work.doi,
                        "journal": work.journal,
                        "publisher": work.publisher,
                        "authors": work.authors or [],
                        "all_dspace_authors": [],
                        "main_author": (work.authors or [None])[0],
                        "affiliations": affiliations,
                        "keywords": work.keywords or [],
                        "publication_year": work.publication_year,
                        "document_type": work.document_type,
                        "open_access_status": work.open_access_status,
                        "citation_count": work.citation_count,
                        "primary_source_url": publication_url,
                        "uri": publication_url,
                        "source_count": work.source_count,
                        "source_names": work.source_names or [],
                        "openalex_institutions": work.affiliations or [],
                    },
                    "faculty": faculty,
                    "department": department,
                }
            )
        return dashboard_rows

    def _researcher_affiliations_by_work_id(self, session) -> dict[int, list[str]]:
        rows = session.execute(
            select(ResearcherWork.work_id, Researcher.affiliations)
            .join(Researcher, Researcher.id == ResearcherWork.researcher_id)
            .where(Researcher.affiliations.is_not(None))
        ).all()
        affiliations_by_work_id: dict[int, list[str]] = {}
        for work_id, affiliations in rows:
            if not work_id or not isinstance(affiliations, list):
                continue
            affiliations_by_work_id[work_id] = self._merge_text_lists(
                affiliations_by_work_id.get(work_id),
                affiliations,
            )
        return affiliations_by_work_id

    def _merge_text_lists(self, *values: list[str] | None) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, list):
                continue
            for item in value:
                text = " ".join(str(item or "").split()).strip()
                if not text:
                    continue
                key = text.lower()
                if key in seen:
                    continue
                seen.add(key)
                merged.append(text)
        return merged

    def _dashboard_rows_from_article_metadata(self) -> list[dict]:
        records = self._article_metadata_records()
        if not records:
            return []

        session = SessionLocal()
        try:
            works = list(session.execute(select(Work)).scalars())
            source_rows = session.execute(
                select(SourceRecord, Work).join(Work, SourceRecord.work_id == Work.id)
            ).all()
        finally:
            session.close()

        works_by_doi: dict[str, Work] = {}
        works_by_title: dict[str, Work] = {}
        for work in works:
            doi = self._normalize_doi_key(work.doi)
            if doi and doi not in works_by_doi:
                works_by_doi[doi] = work
            title_key = self._title_fingerprint(work.title)
            if title_key and title_key not in works_by_title:
                works_by_title[title_key] = work

        source_records_by_doi: dict[str, tuple[SourceRecord, Work]] = {}
        source_records_by_title: dict[str, tuple[SourceRecord, Work]] = {}
        source_records_by_url: dict[str, tuple[SourceRecord, Work]] = {}
        for source_record, source_work in source_rows:
            doi_key = self._normalize_doi_key(source_record.doi or source_work.doi)
            if doi_key and doi_key not in source_records_by_doi:
                source_records_by_doi[doi_key] = (source_record, source_work)
            title_key = self._title_fingerprint(source_record.title or source_work.title)
            if title_key and title_key not in source_records_by_title:
                source_records_by_title[title_key] = (source_record, source_work)
            for url in (
                source_record.source_url,
                self._dspace_uri_from_source_record_id(source_record.source_record_id),
            ):
                url_key = self._normalize_url_key(url)
                if url_key and url_key not in source_records_by_url:
                    source_records_by_url[url_key] = (source_record, source_work)

        dashboard_rows: list[dict] = []
        for record in records:
            title = record.get("Article Title") or "Untitled publication"
            doi = (record.get("DOI") or "").strip()
            record_uri = self._first_url([record.get("Article URI")])
            title_key = self._title_fingerprint(title)
            doi_key = self._normalize_doi_key(doi)
            work = works_by_doi.get(doi_key) if doi_key else None
            if work is None and title_key:
                work = works_by_title.get(title_key)
            source_match = source_records_by_doi.get(doi_key) if doi_key else None
            if source_match is None:
                source_match = source_records_by_url.get(self._normalize_url_key(record_uri))
            if source_match is None and title_key:
                source_match = source_records_by_title.get(title_key)
            matched_source_record = source_match[0] if source_match is not None else None
            matched_source_work = source_match[1] if source_match is not None else None
            if work is None:
                work = matched_source_work

            main_author = record.get("Main Author") or None
            affiliations = [
                value.strip()
                for value in (
                    record.get("Main Institution"),
                    record.get("Collaborating Institution"),
                    record.get("Institution Affiliation"),
                )
                if isinstance(value, str) and value.strip()
            ]
            source_payload = matched_source_record.raw_payload if matched_source_record is not None else {}
            source_url = (
                self._source_record_url(matched_source_record, work, source_payload)
                if matched_source_record is not None and work is not None
                else None
            )
            dashboard_rows.append(
                {
                    "work": {
                        "id": work.id if work is not None else None,
                        "source_record_id": (
                            matched_source_record.source_record_id
                            if matched_source_record is not None
                            else record.get("Article URI") or doi or title
                        ),
                        "title": title,
                        "doi": doi or (work.doi if work is not None else None) or (
                            matched_source_record.doi if matched_source_record is not None else None
                        ),
                        "journal": work.journal if work is not None else None,
                        "publisher": work.publisher if work is not None else None,
                        "authors": (work.authors if work is not None and work.authors else [main_author] if main_author else []),
                        "all_dspace_authors": [main_author] if main_author else [],
                        "main_author": main_author,
                        "affiliations": affiliations or (work.affiliations if work is not None else []),
                        "keywords": (work.keywords if work is not None and work.keywords else self._source_record_keywords(source_payload)),
                        "publication_year": (
                            (work.publication_year if work is not None else None)
                            or (matched_source_record.publication_year if matched_source_record is not None else None)
                        ),
                        "document_type": (
                            (work.document_type if work is not None else None)
                            or self._source_record_document_type(source_payload)
                        ),
                        "open_access_status": (
                            (work.open_access_status if work is not None else None)
                            or self._source_record_open_access_status(source_payload)
                            or "repository"
                        ),
                        "citation_count": (
                            (work.citation_count if work is not None else None)
                            or (matched_source_record.citation_count if matched_source_record is not None else None)
                            or self._source_record_citation_count(source_payload)
                            or 0
                        ),
                        "primary_source_url": record_uri or source_url or (self._publication_url(work) if work is not None else None),
                        "uri": record_uri or source_url or (self._publication_url(work) if work is not None else None),
                        "source_count": work.source_count if work is not None else 1,
                        "source_names": (
                            work.source_names
                            if work is not None and work.source_names
                            else [matched_source_record.source_name] if matched_source_record is not None
                            else ["dspace"]
                        ),
                        "openalex_institutions": [],
                    },
                    "faculty": record.get("Faculty") or "Unmapped",
                    "department": record.get("Department") or "Unmapped",
                }
            )
        return dashboard_rows

    def _filter_annotated_works(
        self,
        annotated: list[dict],
        faculty: str | None,
        department: str | None,
        article_only: bool = False,
        publication_years: list[int] | tuple[int, ...] | None = None,
    ) -> list[dict]:
        filtered = annotated
        if faculty and faculty != "All faculties":
            filtered = [item for item in filtered if item["faculty"] == faculty]
        if department and department != "All departments":
            filtered = [item for item in filtered if item["department"] == department]
        if article_only:
            filtered = [item for item in filtered if self._is_article_work(item["work"])]
        if publication_years:
            selected_years = set(self._normalize_publication_years(publication_years))
            filtered = [
                item
                for item in filtered
                if self._normalize_publication_year(
                    self._work_value(item["work"], "publication_year")
                )
                in selected_years
            ]
        return filtered

    def _overview_from_annotated(
        self,
        annotated: list[dict],
        faculty: str | None,
        department: str | None,
    ) -> dict:
        clustered = self._cluster_annotated_works(annotated)
        total_works = len(clustered)
        total_source_records = sum(item["work"].source_count or 0 for item in clustered)
        duplicates_removed = max(total_source_records - total_works, 0)
        latest_run = self.recent_runs(limit=1)
        is_default_scope = (
            (faculty is None or faculty == "All faculties")
            and (department is None or department == "All departments")
        )
        researcher_count = (
            self._default_dashboard_researcher_count()
            if is_default_scope
            else len(self._researchers_from_annotated(clustered, limit=None))
        )
        return {
            "total_works": total_works,
            "total_source_records": total_source_records,
            "duplicates_removed": duplicates_removed,
            "researcher_count": researcher_count,
            "selected_faculty": faculty or "All faculties",
            "selected_department": department or "All departments",
            "latest_run": latest_run[0] if latest_run else None,
        }

    def _kpi_summary_from_annotated(self, annotated: list[dict]) -> dict:
        clustered = self._cluster_annotated_works(annotated)
        publications = len(clustered)
        citations = sum(item["work"].citation_count or 0 for item in clustered)
        h_index = self._calculate_h_index(clustered)
        researchers = len(self._researchers_from_annotated(clustered, limit=None))
        collaborative_publications = sum(
            1 for item in clustered if self._is_collaborative(item["work"])
        )
        collaboration_percentage = (
            round((collaborative_publications / publications) * 100, 1)
            if publications
            else 0.0
        )

        return {
            "publications": publications,
            "citations": citations,
            "h_index": h_index,
            "researchers": researchers,
            "collaboration_percentage": collaboration_percentage,
        }

    def _by_source_from_annotated(self, annotated: list[dict]) -> list[dict]:
        counts: dict[str, int] = {}
        for item in annotated:
            for source_name in item["work"].source_names or []:
                counts[source_name] = counts.get(source_name, 0) + 1
        return self._rank_counts(counts, "source")

    def _by_year_from_annotated(self, annotated: list[dict]) -> list[dict]:
        counts: dict[int, int] = {}
        for item in annotated:
            year = item["work"].publication_year
            if year is not None:
                counts[year] = counts.get(year, 0) + 1
        return [{"year": year, "count": counts[year]} for year in sorted(counts)]

    def _by_faculty_from_annotated(self, annotated: list[dict]) -> list[dict]:
        counts: dict[str, int] = {}
        for item in annotated:
            label = self._visible_mapping_label(item["faculty"])
            if not label:
                continue
            counts[label] = counts.get(label, 0) + 1
        return self._rank_counts(counts, "faculty")

    def _by_department_from_annotated(self, annotated: list[dict]) -> list[dict]:
        counts: dict[str, int] = {}
        for item in annotated:
            label = self._visible_mapping_label(item["department"])
            if not label:
                continue
            counts[label] = counts.get(label, 0) + 1
        return self._rank_counts(counts, "department")

    def _by_document_type_from_annotated(self, annotated: list[dict]) -> list[dict]:
        counts: dict[str, int] = {}
        for item in annotated:
            label = item["work"].document_type or "unknown"
            counts[label] = counts.get(label, 0) + 1
        return self._rank_counts(counts, "document_type")

    def _top_journals_from_annotated(self, annotated: list[dict], limit: int) -> list[dict]:
        counts: dict[str, int] = {}
        for item in annotated:
            journal = item["work"].journal
            if journal:
                counts[journal] = counts.get(journal, 0) + 1
        return self._rank_counts(counts, "journal", limit)

    def _open_access_from_annotated(self, annotated: list[dict]) -> list[dict]:
        counts: dict[str, int] = {}
        for item in annotated:
            status = item["work"].open_access_status or "unknown"
            counts[status] = counts.get(status, 0) + 1
        return self._rank_counts(counts, "open_access_status")

    def _top_keywords_from_annotated(self, annotated: list[dict], limit: int) -> list[dict]:
        counts: dict[str, int] = {}
        for item in annotated:
            for keyword in item["work"].keywords or []:
                cleaned = keyword.strip()
                if cleaned:
                    counts[cleaned] = counts.get(cleaned, 0) + 1
        return self._rank_counts(counts, "topic", limit)

    def _topic_trends_from_annotated(self, annotated: list[dict], limit: int) -> dict:
        top_topics = self._top_keywords_from_annotated(annotated, limit=limit)
        topic_names = [topic["topic"] for topic in top_topics]
        year_set = {
            item["work"].publication_year
            for item in annotated
            if item["work"].publication_year is not None and item["work"].publication_year >= 2020
        }
        years = sorted(year_set)
        series: list[dict] = []

        for topic_name in topic_names:
            counts_by_year = {year: 0 for year in years}
            for item in annotated:
                work = item["work"]
                year = work.publication_year
                if year is None:
                    continue
                if topic_name in (work.keywords or []):
                    counts_by_year[year] = counts_by_year.get(year, 0) + 1

            series.append(
                {
                    "topic": topic_name,
                    "points": [
                        {"year": year, "count": counts_by_year.get(year, 0)}
                        for year in years
                    ],
                }
            )

        return {
            "years": years,
            "series": series,
        }

    def _publications_from_annotated(self, annotated: list[dict], limit: int) -> list[dict]:
        visible_items = [
            item
            for item in annotated
            if self._visible_mapping_label(item["faculty"])
            and self._visible_mapping_label(item["department"])
        ]
        sorted_items = sorted(
            visible_items,
            key=lambda item: (
                item["work"].publication_year if item["work"].publication_year is not None else -1,
                item["work"].updated_at,
            ),
            reverse=True,
        )
        return [
            {
                "id": item["work"].id,
                "title": item["work"].title,
                "faculty": self._visible_mapping_label(item["faculty"]),
                "department": self._visible_mapping_label(item["department"]),
                "doi": item["work"].doi,
                "publication_year": item["work"].publication_year,
                "journal": item["work"].journal,
                "document_type": item["work"].document_type,
                "citation_count": item["work"].citation_count,
                "author_count": len(item["work"].authors or []),
                "source_names": item["work"].source_names or [],
                "open_access_status": item["work"].open_access_status,
            }
            for item in sorted_items[:limit]
        ]

    def _publications_from_dashboard_rows(self, rows: list[dict], limit: int) -> list[dict]:
        visible_rows = [
            item
            for item in rows
            if self._visible_mapping_label(item["faculty"])
            and self._visible_mapping_label(item["department"])
        ]
        sorted_items = sorted(
            visible_rows,
            key=lambda item: (
                item["work"].get("publication_year") if item["work"].get("publication_year") is not None else -1,
                item["work"].get("id") or -1,
            ),
            reverse=True,
        )
        return [
            {
                "id": item["work"].get("id"),
                "title": item["work"].get("title"),
                "faculty": self._visible_mapping_label(item["faculty"]),
                "department": self._visible_mapping_label(item["department"]),
                "doi": item["work"].get("doi"),
                "publication_year": item["work"].get("publication_year"),
                "journal": item["work"].get("journal"),
                "document_type": item["work"].get("document_type"),
                "citation_count": item["work"].get("citation_count"),
                "author_count": len(item["work"].get("authors") or []),
                "source_names": item["work"].get("source_names") or [],
                "open_access_status": item["work"].get("open_access_status"),
                "uri": self._dspace_uri_from_work(item["work"]) or self._publication_url(item["work"]),
                "publication_url": self._dspace_uri_from_work(item["work"]) or self._publication_url(item["work"]),
            }
            for item in sorted_items[:limit]
        ]

    def _researchers_from_annotated(self, annotated: list[dict], limit: int | None) -> list[dict]:
        researcher_index: dict[str, dict] = {}
        for item in annotated:
            work = item["work"]
            for author in self._iter_author_entries(work.authors or []):
                author_key, author_display = self._cluster_author_identity(author)
                if not author_key:
                    continue
                entry = researcher_index.setdefault(
                    author_key,
                    {
                        "researcher": author_display,
                        "publication_count": 0,
                        "citation_count": 0,
                        "faculty_department_counts": {},
                        "journals": set(),
                        "latest_year": None,
                    },
                )
                entry["researcher"] = self._pick_better_author_name(entry["researcher"], author_display)
                entry["publication_count"] += 1
                entry["citation_count"] += work.citation_count or 0
                faculty_label = self._visible_mapping_label(item["faculty"])
                department_label = self._visible_mapping_label(item["department"])
                if faculty_label:
                    faculty_department = (faculty_label, department_label or "")
                    entry["faculty_department_counts"][faculty_department] = (
                        entry["faculty_department_counts"].get(faculty_department, 0) + 1
                    )
                if work.journal:
                    entry["journals"].add(work.journal)
                if work.publication_year is not None:
                    if entry["latest_year"] is None or work.publication_year > entry["latest_year"]:
                        entry["latest_year"] = work.publication_year

        researchers = []
        for payload in researcher_index.values():
            faculty_department = self._top_faculty_department(payload["faculty_department_counts"])
            faculty = faculty_department[0] if faculty_department else ""
            department = faculty_department[1] if faculty_department else ""
            researchers.append(
                {
                    "researcher": payload["researcher"],
                    "publication_count": payload["publication_count"],
                    "citation_count": payload["citation_count"],
                    "faculties": [faculty] if faculty else [],
                    "departments": [department] if department else [],
                    "journal_count": len(payload["journals"]),
                    "latest_year": payload["latest_year"],
                }
            )
        researchers.sort(
            key=lambda item: (-item["publication_count"], -item["citation_count"], item["researcher"].lower())
        )
        return researchers if limit is None else researchers[:limit]

    def _researchers_from_dashboard_rows(self, rows: list[dict], limit: int | None) -> list[dict]:
        published_in_by_author_key = self._researcher_published_in_by_author_key(rows)
        researcher_index: dict[str, dict] = {}
        for item in rows:
            work = item["work"]
            for author in self._iter_author_entries(work.get("authors") or []):
                author_key, author_display = self._cluster_author_identity(author)
                if not author_key:
                    continue
                entry = researcher_index.setdefault(
                    author_key,
                    {
                        "researcher": author_display,
                        "publication_count": 0,
                        "citation_count": 0,
                        "faculty_department_counts": {},
                        "journals": set(),
                        "latest_year": None,
                    },
                )
                entry["researcher"] = self._pick_better_author_name(entry["researcher"], author_display)
                entry["publication_count"] += 1
                entry["citation_count"] += work.get("citation_count") or 0
                faculty_label = self._visible_mapping_label(item["faculty"])
                department_label = self._visible_mapping_label(item["department"])
                if faculty_label:
                    faculty_department = (faculty_label, department_label or "")
                    entry["faculty_department_counts"][faculty_department] = (
                        entry["faculty_department_counts"].get(faculty_department, 0) + 1
                    )
                if work.get("journal"):
                    entry["journals"].add(work["journal"])
                if work.get("publication_year") is not None:
                    if entry["latest_year"] is None or work["publication_year"] > entry["latest_year"]:
                        entry["latest_year"] = work["publication_year"]

        researchers = []
        for author_key, payload in researcher_index.items():
            published_in = published_in_by_author_key.get(author_key)
            if published_in is None:
                faculty_department = self._top_faculty_department(payload["faculty_department_counts"])
                faculties = [faculty_department[0]] if faculty_department and faculty_department[0] else []
                departments = [faculty_department[1]] if faculty_department and faculty_department[1] else []
            else:
                faculties = published_in["faculties"]
                departments = published_in["departments"]
            researchers.append(
                {
                    "researcher": payload["researcher"],
                    "publication_count": payload["publication_count"],
                    "citation_count": payload["citation_count"],
                    "faculties": faculties,
                    "departments": departments,
                    "journal_count": len(payload["journals"]),
                    "latest_year": payload["latest_year"],
                }
            )
        researchers.sort(
            key=lambda item: (-item["publication_count"], -item["citation_count"], item["researcher"].lower())
        )
        return researchers if limit is None else researchers[:limit]

    def _researchers_from_article_metadata(
        self,
        faculty: str | None = None,
        department: str | None = None,
        publication_years: list[int] | tuple[int, ...] | None = None,
        article_only: bool = False,
    ) -> list[dict]:
        selected_years = set(self._normalize_publication_years(publication_years))
        researcher_index: dict[str, dict] = {}
        for record in self._article_metadata_records():
            mapped_faculty = record.get("Faculty") or "Unmapped"
            mapped_department = record.get("Department") or "Unmapped"
            if faculty and faculty != "All faculties" and mapped_faculty != faculty:
                continue
            if department and department != "All departments" and mapped_department != department:
                continue
            faculty_label = self._visible_mapping_label(mapped_faculty)
            department_label = self._visible_mapping_label(mapped_department)
            if not faculty_label:
                continue

            year = self._metadata_publication_year(record)
            if selected_years and year not in selected_years:
                continue

            main_author = record.get("Main Author") or ""
            author_key, author_display = self._cluster_author_identity(main_author)
            if not author_key:
                continue
            author_key = self._metadata_author_summary_key(main_author, author_key, researcher_index)
            entry = researcher_index.setdefault(
                author_key,
                {
                    "researcher": author_display,
                    "publication_count": 0,
                    "citation_count": 0,
                    "faculties": set(),
                    "departments": set(),
                    "journals": set(),
                    "latest_year": None,
                },
            )
            entry["researcher"] = self._pick_better_author_name(entry["researcher"], author_display)
            entry["publication_count"] += 1
            entry["faculties"].add(faculty_label)
            if department_label:
                entry["departments"].add(department_label)
            doi = str(record.get("DOI") or "").strip()
            if doi:
                entry["journals"].add(doi)
            if year is not None and (entry["latest_year"] is None or year > entry["latest_year"]):
                entry["latest_year"] = year

        researchers = [
            {
                "researcher": payload["researcher"],
                "publication_count": payload["publication_count"],
                "citation_count": payload["citation_count"],
                "faculties": sorted(payload["faculties"]),
                "departments": sorted(payload["departments"]),
                "journal_count": len(payload["journals"]),
                "latest_year": payload["latest_year"],
            }
            for payload in researcher_index.values()
        ]
        researchers.sort(
            key=lambda item: (-item["publication_count"], -item["citation_count"], item["researcher"].lower())
        )
        return researchers

    def _is_openalex_unmapped_filter(
        self,
        faculty: str | None = None,
        department: str | None = None,
    ) -> bool:
        expected = self._normalize_search_text(self._openalex_unmapped_label)
        return any(
            self._normalize_search_text(value) == expected
            for value in (faculty, department)
            if value
        )

    def _openalex_unmapped_researcher_rows(
        self,
        researcher_query: str,
        publication_years: list[int] | tuple[int, ...] | None = None,
        article_only: bool = False,
    ) -> list[dict]:
        if publication_years or article_only:
            return []

        mapped_counts = self._mapped_publication_counts_by_author_key_from_article_metadata()
        source_rows = self._filter_researcher_results(
            self._researchers_from_all_works(
                None,
                allow_sync_rebuild=False,
            ),
            researcher_query,
        )
        rows: list[dict] = []
        for item in source_rows:
            author_key, _ = self._cluster_author_identity(item.get("researcher"))
            mapped_total = int(mapped_counts.get(author_key) or 0)
            total = int(item.get("publication_count") or 0)
            unmapped_total = max(total - mapped_total, 0)
            if unmapped_total <= 0:
                continue
            source_row = dict(item)
            source_row.update(
                {
                    "publication_count": unmapped_total,
                    "citation_count": 0,
                    "faculties": [self._openalex_unmapped_label],
                    "departments": [self._openalex_unmapped_label],
                    "journal_count": 0,
                    "mapped_publication_count": mapped_total,
                    "openalex_publication_count": total,
                }
            )
            source_row.pop("h_index", None)
            rows.append(source_row)
        return rows

    def _metadata_author_summary_key(
        self,
        author_name: str,
        author_key: str,
        researcher_index: dict[str, dict],
    ) -> str:
        parts = [part.strip() for part in str(author_name or "").split(",", 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            normalized_parts = [self._normalize_search_text(part) for part in parts]
            if all(normalized_parts):
                return "comma-pair:" + "|".join(sorted(normalized_parts))
        if author_key in researcher_index:
            return author_key
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return author_key
        reversed_key, _ = self._cluster_author_identity(f"{parts[1]} {parts[0]}")
        if reversed_key and reversed_key in researcher_index:
            return reversed_key
        return author_key

    def _metadata_publication_year(self, record: dict) -> int | None:
        for key in ("Publication Year", "Year", "Date", "Issued", "Published"):
            year = self._normalize_publication_year(record.get(key))
            if year is not None:
                return year
        lookup = self._article_metadata_year_lookup()
        doi = str(record.get("DOI") or "").strip().lower()
        if doi:
            year = lookup.get(f"doi:{doi}")
            if year is not None:
                return year
        title_key = self._title_fingerprint(record.get("Article Title"))
        if title_key:
            return lookup.get(f"title:{title_key}")
        return None

    def _metadata_record_is_article(self, record: dict) -> bool:
        lookup = self._article_metadata_article_lookup()
        doi = str(record.get("DOI") or "").strip().lower()
        if doi and f"doi:{doi}" in lookup:
            return True
        title_key = self._title_fingerprint(record.get("Article Title"))
        if title_key and f"title:{title_key}" in lookup:
            return True
        uri_key = self._normalize_url_key(record.get("Article URI"))
        if uri_key and f"uri:{uri_key}" in lookup:
            return True
        return False

    def _article_metadata_article_lookup(self) -> set[str]:
        now = time.monotonic()
        cached = self._article_metadata_article_lookup_cache
        if cached is not None and now < cached[0]:
            return cached[1]

        lookup: set[str] = set()
        session = SessionLocal()
        try:
            rows = session.execute(
                select(
                    SourceRecord.title,
                    SourceRecord.doi,
                    SourceRecord.publication_year,
                    SourceRecord.raw_payload,
                )
                .where(SourceRecord.source_name == "dspace")
            ).all()
        finally:
            session.close()

        year_lookup: dict[str, int] = {}
        for title, doi, publication_year, payload in rows:
            payload = payload if isinstance(payload, dict) else {}
            year = self._normalize_publication_year(publication_year)
            doi_key = str(doi or "").strip().lower()
            title_key = self._title_fingerprint(title)
            if year is not None:
                if doi_key:
                    year_lookup.setdefault(f"doi:{doi_key}", year)
                if title_key:
                    year_lookup.setdefault(f"title:{title_key}", year)

            source_document_type = self._source_record_document_type(payload)
            if not self._is_article_document_type(source_document_type):
                continue

            if doi_key:
                lookup.add(f"doi:{doi_key}")
            if title_key:
                lookup.add(f"title:{title_key}")
            for identifier in self._dspace_values(payload, "identifier"):
                uri_key = self._normalize_url_key(identifier)
                if uri_key:
                    lookup.add(f"uri:{uri_key}")

        self._article_metadata_article_lookup_cache = (
            now + self._dashboard_cache_ttl_seconds,
            lookup,
        )
        if year_lookup:
            self._article_metadata_year_lookup_cache = (
                now + self._dashboard_cache_ttl_seconds,
                year_lookup,
            )
        return lookup

    def _article_metadata_year_lookup(self) -> dict[str, int]:
        now = time.monotonic()
        cached = self._article_metadata_year_lookup_cache
        if cached is not None and now < cached[0]:
            return cached[1]

        lookup: dict[str, int] = {}
        session = SessionLocal()
        try:
            rows = session.execute(
                select(
                    SourceRecord.title,
                    SourceRecord.doi,
                    SourceRecord.publication_year,
                )
                .where(SourceRecord.source_name == "dspace")
            ).all()
        finally:
            session.close()

        for source_title, source_doi, source_year in rows:
            year = self._normalize_publication_year(source_year)
            if year is None:
                continue
            doi_key = str(source_doi or "").strip().lower()
            if doi_key:
                lookup.setdefault(f"doi:{doi_key}", year)
            title_key = self._title_fingerprint(source_title)
            if title_key:
                lookup.setdefault(f"title:{title_key}", year)

        self._article_metadata_year_lookup_cache = (
            now + self._dashboard_cache_ttl_seconds,
            lookup,
        )
        return lookup

    def _researcher_rows_cache_ready(self) -> bool:
        cached = self._researcher_rows_cache
        return cached is not None and time.monotonic() < cached[0]

    def _refresh_researcher_rows_cache_async(self) -> None:
        with self._researcher_rows_cache_lock:
            if self._researcher_rows_refreshing:
                return
            self._researcher_rows_refreshing = True

        def refresh() -> None:
            try:
                self._researcher_rows()
            except Exception:
                logger.exception("Background researcher rows cache build failed")
            finally:
                with self._researcher_rows_cache_lock:
                    self._researcher_rows_refreshing = False

        threading.Thread(target=refresh, daemon=True).start()

    def _researcher_rows(self) -> list[dict]:
        now = time.monotonic()
        cached = self._researcher_rows_cache
        if cached is not None and now < cached[0]:
            return cached[1]
        rows = self._build_researcher_rows()
        self._researcher_rows_cache = (
            time.monotonic() + self._dashboard_cache_ttl_seconds,
            rows,
        )
        self._researcher_rows_by_author_key = self._index_researcher_rows_by_author_key(rows)
        return rows

    def _index_researcher_rows_by_author_key(self, rows: list[dict]) -> dict[str, list[dict]]:
        index: dict[str, list[dict]] = {}
        for item in rows:
            work = item.get("work") or {}
            for author in self._iter_author_entries(work.get("authors") or []):
                author_key, _ = self._cluster_author_identity(author)
                if not author_key:
                    continue
                index.setdefault(author_key, []).append(item)
        return index

    def _build_researcher_rows(self) -> list[dict]:
        session = SessionLocal()
        try:
            work_rows = session.execute(
                select(
                    Work.id,
                    Work.title,
                    Work.abstract,
                    Work.doi,
                    Work.authors,
                    Work.affiliations,
                    Work.keywords,
                    Work.journal,
                    Work.publisher,
                    Work.document_type,
                    Work.open_access_status,
                    Work.citation_count,
                    Work.publication_year,
                    Work.primary_source_url,
                    Work.source_names,
                )
            ).all()
        finally:
            session.close()

        faculty_department_by_work_id = self._researcher_faculty_department_by_work_id()
        author_keys_by_work_id: dict[int, set[str]] = {}
        rows: list[dict] = []
        for (
            work_id,
            title,
            abstract,
            doi,
            authors,
            affiliations,
            keywords,
            journal,
            publisher,
            document_type,
            open_access_status,
            citation_count,
            publication_year,
            primary_source_url,
            source_names,
        ) in work_rows:
            if not authors:
                continue
            author_keys_by_work_id[work_id] = {
                author_key
                for author_key, _ in (
                    self._cluster_author_identity(author)
                    for author in self._iter_author_entries(authors)
                )
                if author_key
            }
            faculty, department = faculty_department_by_work_id.get(work_id, ("Unmapped", "Unmapped"))
            rows.append(
                {
                    "work": {
                        "id": work_id,
                        "title": title,
                        "abstract": abstract,
                        "doi": doi,
                        "authors": authors,
                        "affiliations": affiliations or [],
                        "keywords": keywords or [],
                        "journal": journal,
                        "publisher": publisher,
                        "document_type": document_type,
                        "open_access_status": open_access_status,
                        "citation_count": citation_count,
                        "publication_year": publication_year,
                        "primary_source_url": primary_source_url,
                        "source_names": source_names or [],
                    },
                    "faculty": faculty,
                    "department": department,
                }
            )
        for row in self._dashboard_rows():
            work = row.get("work") or {}
            main_author = work.get("main_author")
            author_key, _ = self._cluster_author_identity(main_author)
            if not author_key:
                continue
            work_id = work.get("id")
            try:
                normalized_work_id = int(work_id)
            except (TypeError, ValueError):
                normalized_work_id = None
            if normalized_work_id is not None and author_key in author_keys_by_work_id.get(normalized_work_id, set()):
                continue
            rows.append(
                {
                    "work": {
                        "id": normalized_work_id,
                        "title": work.get("title"),
                        "doi": work.get("doi"),
                        "authors": [main_author],
                        "affiliations": work.get("affiliations") or [],
                        "keywords": work.get("keywords") or [],
                        "journal": work.get("journal"),
                        "document_type": work.get("document_type"),
                        "open_access_status": work.get("open_access_status"),
                        "citation_count": work.get("citation_count"),
                        "publication_year": work.get("publication_year"),
                        "primary_source_url": work.get("primary_source_url"),
                        "source_names": work.get("source_names") or ["dspace"],
                    },
                    "faculty": row.get("faculty") or "Unmapped",
                    "department": row.get("department") or "Unmapped",
                }
            )
        return rows

    def _researcher_faculty_department_by_work_id(self) -> dict[int, tuple[str, str]]:
        mapping: dict[int, tuple[str, str]] = {}
        for row in self._dashboard_rows():
            work = row.get("work") or {}
            work_id = work.get("id")
            if work_id is None:
                continue
            try:
                normalized_work_id = int(work_id)
            except (TypeError, ValueError):
                continue
            faculty = row.get("faculty") or "Unmapped"
            department = row.get("department") or "Unmapped"
            mapping[normalized_work_id] = (faculty, department)
        return mapping

    def _researchers_from_all_works(
        self,
        limit: int | None,
        allow_sync_rebuild: bool = False,
    ) -> list[dict]:
        now = time.monotonic()
        cached = self._researchers_all_works_cache
        if cached is not None and now < cached[0]:
            self._last_researchers_cache_status = self._researchers_all_works_cache_source or "HIT"
            full_list = self._clean_researcher_results(cached[1])
            return full_list if limit is None else full_list[:limit]

        self._load_researchers_cache_snapshot()
        cached = self._researchers_all_works_cache
        if cached is not None:
            self._last_researchers_cache_status = "SNAPSHOT"
            full_list = self._clean_researcher_results(cached[1])
            return full_list if limit is None else full_list[:limit]

        self._last_researchers_cache_status = "REBUILDING"
        if not allow_sync_rebuild:
            fallback = self._researchers_from_works_fast(limit)
            if fallback:
                self._last_researchers_cache_status = "FAST"
                return fallback
            return []

        with self._researchers_cache_lock:
            if self._researchers_refreshing:
                should_wait = True
            else:
                should_wait = False
                self._researchers_refreshing = True
        if should_wait:
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                cached = self._researchers_all_works_cache
                if cached is not None:
                    self._last_researchers_cache_status = "HIT"
                    full_list = self._clean_researcher_results(cached[1])
                    return full_list if limit is None else full_list[:limit]
                time.sleep(0.25)
            return []
        try:
            full_list = self._build_researchers_cache()
            self._last_researchers_cache_status = "REBUILT"
            return full_list if limit is None else full_list[:limit]
        finally:
            with self._researchers_cache_lock:
                self._researchers_refreshing = False

    def _refresh_researchers_cache_async(self) -> None:
        with self._researchers_cache_lock:
            if self._researchers_refreshing:
                return
            self._researchers_refreshing = True

        def refresh() -> None:
            try:
                self._build_researchers_cache()
            except Exception:
                logger.exception("Background researchers cache build failed")
            finally:
                with self._researchers_cache_lock:
                    self._researchers_refreshing = False

        threading.Thread(target=refresh, daemon=True).start()

    def _researchers_from_works_fast(
        self,
        limit: int | None,
        publication_years: list[int] | tuple[int, ...] | None = None,
    ) -> list[dict]:
        selected_years = set(self._normalize_publication_years(publication_years))
        session = SessionLocal()
        try:
            query = (
                select(
                    Work.authors,
                    Work.citation_count,
                    Work.journal,
                    Work.publication_year,
                )
                .where(Work.authors.isnot(None))
            )
            if selected_years:
                query = query.where(Work.publication_year.in_(selected_years))
            rows = session.execute(query).all()
        finally:
            session.close()

        researcher_index: dict[str, dict] = {}
        for authors, citation_count, journal, publication_year in rows:
            for author in self._iter_author_entries(authors or []):
                author_key, author_display = self._cluster_author_identity(author)
                if not author_key:
                    continue
                entry = researcher_index.setdefault(
                    author_key,
                    {
                        "researcher": author_display,
                        "publication_count": 0,
                        "citation_count": 0,
                        "journals": set(),
                        "latest_year": None,
                    },
                )
                entry["researcher"] = self._pick_better_author_name(entry["researcher"], author_display)
                entry["publication_count"] += 1
                entry["citation_count"] += citation_count or 0
                if journal:
                    entry["journals"].add(journal)
                normalized_year = self._normalize_publication_year(publication_year)
                if normalized_year is not None and (
                    entry["latest_year"] is None or normalized_year > entry["latest_year"]
                ):
                    entry["latest_year"] = normalized_year

        published_in_by_author_key = self._researcher_published_in_by_author_key()
        mapped_counts_by_author_key = self._mapped_publication_counts_by_author_key_from_article_metadata()
        researchers = []
        for author_key, payload in researcher_index.items():
            published_in = published_in_by_author_key.get(author_key, {})
            faculties = published_in.get("faculties") or [self._openalex_unmapped_label]
            departments = published_in.get("departments") or [self._openalex_unmapped_label]
            mapped_total = int(mapped_counts_by_author_key.get(author_key) or 0)
            if int(payload["publication_count"] or 0) > mapped_total:
                if self._openalex_unmapped_label not in faculties:
                    faculties = [*faculties, self._openalex_unmapped_label]
                if self._openalex_unmapped_label not in departments:
                    departments = [*departments, self._openalex_unmapped_label]
            researchers.append(
                {
                    "researcher": payload["researcher"],
                    "publication_count": payload["publication_count"],
                    "citation_count": payload["citation_count"],
                    "faculties": faculties,
                    "departments": departments,
                    "journal_count": len(payload["journals"]),
                    "latest_year": payload["latest_year"],
                }
            )
        researchers.sort(
            key=lambda item: (-item["publication_count"], -item["citation_count"], item["researcher"].lower())
        )
        full_list = self._clean_researcher_results(researchers)
        if not selected_years:
            self._researchers_all_works_cache = (
                time.monotonic() + self._researchers_cache_ttl_seconds,
                full_list,
            )
            self._researchers_all_works_cache_source = "FAST"
            self._researchers_by_author_key = self._index_researcher_summaries_by_author_key(full_list)
            self._save_researchers_cache_snapshot(full_list)
        return full_list if limit is None else full_list[:limit]

    def _researchers_from_source_records_fast(
        self,
        limit: int | None,
        *,
        faculty: str | None = None,
        department: str | None = None,
        publication_years: list[int] | tuple[int, ...] | None = None,
        article_only: bool = False,
    ) -> list[dict]:
        selected_years = set(self._normalize_publication_years(publication_years))
        cache_key = (
            faculty,
            department,
            tuple(sorted(selected_years)),
            bool(article_only),
        )
        now = time.monotonic()
        cached = self._filtered_researchers_cache.get(cache_key)
        if cached is not None and now < cached[0]:
            rows = cached[1]
            return rows if limit is None else rows[:limit]

        target_limit = limit or 50
        fetch_limit = max(target_limit * 20, 1000)
        max_fetch_limit = 20000
        researcher_index: dict[str, dict] = {}
        seen_work_author_keys: set[tuple[int | str, str]] = set()
        sort_year = func.coalesce(SourceRecord.publication_year, Work.publication_year)

        while True:
            query = (
                select(Work, SourceRecord)
                .join(SourceRecord, SourceRecord.work_id == Work.id)
                .where(SourceRecord.source_name == "dspace")
                .order_by(sort_year.desc(), SourceRecord.id.desc())
                .limit(fetch_limit)
            )
            if selected_years:
                query = query.where(sort_year.in_(selected_years))
            if article_only:
                query = query.where(
                    Work.document_type.ilike("%article%")
                    | SourceRecord.raw_payload["type"].as_string().ilike("%article%")
                    | SourceRecord.raw_payload["subtype"].as_string().ilike("%article%")
                    | SourceRecord.raw_payload["subtypeDescription"].as_string().ilike("%article%")
                    | SourceRecord.raw_payload["dc:type"].as_string().ilike("%article%")
                )

            session = SessionLocal()
            try:
                rows = session.execute(query).all()
            finally:
                session.close()

            researcher_index.clear()
            seen_work_author_keys.clear()
            for work, source_record in rows:
                payload = source_record.raw_payload or {}
                title = source_record.title or self._source_record_title(payload) or work.title
                faculty_value, department_value = self._dspace_faculty_department(
                    dspace_payload=payload,
                    title=title,
                    journal=work.journal,
                    publisher=work.publisher,
                    keywords=work.keywords or [],
                )
                faculty_label = self._visible_mapping_label(faculty_value)
                department_label = self._visible_mapping_label(department_value)
                if faculty and faculty != "All faculties" and faculty_label != faculty:
                    continue
                if department and department != "All departments" and department_label != department:
                    continue

                authors = self._iter_author_entries(work.authors or self._dspace_creators(payload))
                if not authors:
                    continue
                work_key: int | str = work.id or source_record.id
                publication_year = self._normalize_publication_year(source_record.publication_year or work.publication_year)
                for author in authors:
                    author_key, author_display = self._cluster_author_identity(author)
                    if not author_key:
                        continue
                    seen_key = (work_key, author_key)
                    if seen_key in seen_work_author_keys:
                        continue
                    seen_work_author_keys.add(seen_key)
                    entry = researcher_index.setdefault(
                        author_key,
                        {
                            "researcher": author_display,
                            "publication_count": 0,
                            "citation_count": 0,
                            "faculties": set(),
                            "departments": set(),
                            "journals": set(),
                            "latest_year": None,
                        },
                    )
                    entry["researcher"] = self._pick_better_author_name(entry["researcher"], author_display)
                    entry["publication_count"] += 1
                    entry["citation_count"] += work.citation_count or source_record.citation_count or 0
                    if faculty_label:
                        entry["faculties"].add(faculty_label)
                    if department_label:
                        entry["departments"].add(department_label)
                    if work.journal:
                        entry["journals"].add(work.journal)
                    if publication_year is not None and (
                        entry["latest_year"] is None or publication_year > entry["latest_year"]
                    ):
                        entry["latest_year"] = publication_year

            if len(researcher_index) >= target_limit or len(rows) < fetch_limit or fetch_limit >= max_fetch_limit:
                break
            fetch_limit = min(fetch_limit * 2, max_fetch_limit)

        researchers = [
            {
                "researcher": payload["researcher"],
                "publication_count": payload["publication_count"],
                "citation_count": payload["citation_count"],
                "faculties": sorted(payload["faculties"]) or [self._openalex_unmapped_label],
                "departments": sorted(payload["departments"]) or [self._openalex_unmapped_label],
                "journal_count": len(payload["journals"]),
                "latest_year": payload["latest_year"],
            }
            for payload in researcher_index.values()
        ]
        researchers.sort(
            key=lambda item: (-item["publication_count"], -item["citation_count"], item["researcher"].lower())
        )
        researchers = self._clean_researcher_results(researchers)
        self._filtered_researchers_cache[cache_key] = (
            time.monotonic() + self._dashboard_cache_ttl_seconds,
            researchers,
        )
        return researchers if limit is None else researchers[:limit]

    def _build_researchers_cache(self) -> list[dict]:
        researcher_rows = self._researcher_rows()
        published_in_by_author_key = self._researcher_published_in_by_author_key()
        researcher_index: dict[str, dict] = {}
        for item in researcher_rows:
            work = item["work"]
            faculty = item["faculty"]
            department = item["department"]
            for author in self._iter_author_entries(work.get("authors") or []):
                author_key, author_display = self._cluster_author_identity(author)
                if not author_key:
                    continue
                entry = researcher_index.setdefault(
                    author_key,
                    {
                        "researcher": author_display,
                        "publication_count": 0,
                        "citation_count": 0,
                        "faculty_department_counts": {},
                        "journals": set(),
                        "latest_year": None,
                    },
                )
                entry["researcher"] = self._pick_better_author_name(entry["researcher"], author_display)
                entry["publication_count"] += 1
                entry["citation_count"] += work.get("citation_count") or 0
                faculty_label = self._visible_mapping_label(faculty)
                department_label = self._visible_mapping_label(department)
                if faculty_label:
                    faculty_department = (faculty_label, department_label or "")
                    entry["faculty_department_counts"][faculty_department] = (
                        entry["faculty_department_counts"].get(faculty_department, 0) + 1
                    )
                if work.get("journal"):
                    entry["journals"].add(work["journal"])
                if work.get("publication_year") is not None:
                    if entry["latest_year"] is None or work["publication_year"] > entry["latest_year"]:
                        entry["latest_year"] = work["publication_year"]

        researchers = []
        for author_key, payload in researcher_index.items():
            faculties = sorted(
                {
                    faculty
                    for faculty, _ in payload["faculty_department_counts"]
                    if faculty
                }
            )
            departments = sorted(
                {
                    department
                    for _, department in payload["faculty_department_counts"]
                    if department
                }
            )
            published_in = published_in_by_author_key.get(author_key)
            if published_in is not None:
                faculties = sorted({*faculties, *published_in["faculties"]})
                departments = sorted({*departments, *published_in["departments"]})
            researchers.append(
                {
                    "researcher": payload["researcher"],
                    "publication_count": payload["publication_count"],
                    "citation_count": payload["citation_count"],
                    "faculties": faculties,
                    "departments": departments,
                    "journal_count": len(payload["journals"]),
                    "latest_year": payload["latest_year"],
                }
            )
        researchers.sort(
            key=lambda item: (-item["publication_count"], -item["citation_count"], item["researcher"].lower())
        )
        researchers = self._clean_researcher_results(researchers)
        self._researchers_all_works_cache = (
            time.monotonic() + self._researchers_cache_ttl_seconds,
            researchers,
        )
        self._researchers_all_works_cache_source = "REBUILT"
        self._researchers_by_author_key = self._index_researcher_summaries_by_author_key(researchers)
        self._save_researchers_cache_snapshot(researchers)
        return researchers

    def _index_researcher_summaries_by_author_key(self, researchers: list[dict]) -> dict[str, dict]:
        index: dict[str, dict] = {}
        for item in researchers:
            author_key, _ = self._cluster_author_identity(item.get("researcher"))
            if author_key and author_key not in index:
                index[author_key] = item
        return index

    def _researcher_published_in_by_author_key(self, rows: list[dict] | None = None) -> dict[str, dict[str, list[str]]]:
        published_in: dict[str, dict[str, set[str]]] = {}
        if rows is None:
            for record in self._article_metadata_records():
                author_keys = self._metadata_author_alias_keys(record.get("Main Author"))
                if not author_keys:
                    continue
                faculty = self._visible_mapping_label(record.get("Faculty"))
                department = self._visible_mapping_label(record.get("Department")) or ""
                if not faculty and not department:
                    continue
                for author_key in author_keys:
                    entry = published_in.setdefault(author_key, {"faculties": set(), "departments": set()})
                    if faculty:
                        entry["faculties"].add(faculty)
                    if department:
                        entry["departments"].add(department)
            return {
                author_key: {
                    "faculties": sorted(values["faculties"]),
                    "departments": sorted(values["departments"]),
                }
                for author_key, values in published_in.items()
            }

        for row in rows if rows is not None else self._dashboard_rows():
            work = row.get("work") or {}
            faculty = self._visible_mapping_label(row.get("faculty"))
            department = self._visible_mapping_label(row.get("department")) or ""
            if not faculty and not department:
                continue
            authors = self._iter_author_entries(work.get("authors") or [work.get("main_author")])
            for author in authors:
                author_key, _ = self._cluster_author_identity(author)
                if not author_key:
                    continue
                entry = published_in.setdefault(author_key, {"faculties": set(), "departments": set()})
                if faculty:
                    entry["faculties"].add(faculty)
                if department:
                    entry["departments"].add(department)
        return {
            author_key: {
                "faculties": sorted(values["faculties"]),
                "departments": sorted(values["departments"]),
            }
            for author_key, values in published_in.items()
        }

    def _top_faculty_department(self, counts: dict[tuple[str, str], int]) -> tuple[str, str] | None:
        if not counts:
            return None
        return sorted(
            counts,
            key=lambda item: (
                -counts[item],
                not item[0],
                not item[1],
                item[0],
                item[1],
            ),
        )[0]

    def _filter_researcher_results(self, researchers: list[dict], researcher_query: str) -> list[dict]:
        if not researcher_query:
            return researchers
        normalized_query = self._normalize_search_text(researcher_query)
        query_key, _ = self._cluster_author_identity(researcher_query)
        matches = [
            item
            for item in researchers
            if self._researcher_query_matches(
                item.get("researcher"),
                normalized_query,
                query_key,
            )
        ]
        return sorted(
            matches,
            key=lambda item: (
                self._researcher_search_score(item.get("researcher"), normalized_query, query_key),
                self._researcher_sort_name(item.get("researcher")),
            ),
        )

    def _limit_researcher_results(self, researchers: list[dict], limit: int | None) -> list[dict]:
        return researchers if limit is None else researchers[:limit]

    def _rank_researcher_results(self, researchers: list[dict], researcher_query: str) -> list[dict]:
        normalized_query = self._normalize_search_text(researcher_query)
        query_key, _ = self._cluster_author_identity(researcher_query)
        if normalized_query:
            return sorted(
                researchers,
                key=lambda item: (
                    self._researcher_search_score(item.get("researcher"), normalized_query, query_key),
                    -(item.get("publication_count") or 0),
                    -(item.get("citation_count") or 0),
                    self._researcher_sort_name(item.get("researcher")),
                ),
            )
        return sorted(
            researchers,
            key=lambda item: (
                -(item.get("publication_count") or 0),
                -(item.get("citation_count") or 0),
                self._researcher_sort_name(item.get("researcher")),
            ),
        )

    def _researcher_query_matches(
        self,
        researcher_name: str | None,
        normalized_query: str,
        query_key: str,
    ) -> bool:
        normalized_name = self._normalize_search_text(researcher_name)
        if not normalized_query or not normalized_name:
            return False
        if normalized_query in normalized_name:
            return True
        if query_key and self._cluster_author_identity(researcher_name)[0] == query_key:
            return True
        return self._query_tokens_match_name(normalized_query, normalized_name)

    def _query_tokens_match_name(self, normalized_query: str, normalized_name: str) -> bool:
        query_tokens = normalized_query.split()
        name_tokens = normalized_name.split()
        if not query_tokens or not name_tokens:
            return False
        return all(
            any(name_token.startswith(query_token) or query_token in name_token for name_token in name_tokens)
            for query_token in query_tokens
        )

    def _researcher_search_score(
        self,
        researcher_name: str | None,
        normalized_query: str,
        query_key: str,
    ) -> tuple[int, int]:
        normalized_name = self._normalize_search_text(researcher_name)
        if normalized_name == normalized_query:
            return (0, 0)
        if normalized_name.startswith(normalized_query):
            return (1, len(normalized_name))
        if query_key and self._cluster_author_identity(researcher_name)[0] == query_key:
            return (2, len(normalized_name))
        if normalized_query in normalized_name:
            return (3, len(normalized_name))
        return (4, len(normalized_name))

    def _researcher_sort_name(self, value: str | None) -> str:
        text = " ".join(str(value or "").replace(",", " ").split())
        return self._normalize_search_text(text)

    def _normalize_search_text(self, value: str | None) -> str:
        if not value:
            return ""
        normalized = unicodedata.normalize("NFKD", str(value))
        normalized = normalized.encode("ascii", "ignore").decode("ascii")
        normalized = re.sub(r"[^a-z0-9\s]", " ", normalized.lower())
        return " ".join(normalized.split())

    def last_researchers_cache_status(self) -> str:
        return self._last_researchers_cache_status

    def _clean_researcher_results(self, researchers: list[dict]) -> list[dict]:
        cleaned: list[dict] = []
        for item in researchers:
            author_key, author_display = self._cluster_author_identity(item.get("researcher"))
            if not author_key:
                continue
            if author_display != item.get("researcher"):
                item = {**item, "researcher": author_display}
            cleaned.append(item)
        return cleaned

    def _seeded_openalex_metrics_for_researcher(self, researcher_name: str | None) -> dict | None:
        normalized_name = self._normalize_search_text(researcher_name)
        if not normalized_name:
            return None
        metrics = self._seeded_openalex_author_metrics_by_key.get(normalized_name)
        if metrics is not None:
            return dict(metrics)
        author_key, _ = self._cluster_author_identity(researcher_name)
        metrics = self._seeded_openalex_author_metrics_by_key.get(author_key)
        return dict(metrics) if metrics is not None else None

    def _apply_seeded_openalex_metrics(self, researchers: list[dict]) -> list[dict]:
        if not researchers:
            return researchers
        enriched: list[dict] = []
        for item in researchers:
            metrics = self._seeded_openalex_metrics_for_researcher(item.get("researcher"))
            if metrics is None:
                enriched.append(item)
                continue
            updated = dict(item)
            publication_count = metrics.get("publication_count")
            if publication_count is not None:
                updated["publication_count"] = max(
                    int(updated.get("publication_count") or 0),
                    int(publication_count or 0),
                )
                updated = self._append_openalex_unmapped_label_if_needed(
                    updated,
                    int(publication_count or 0),
                )
            citation_count = metrics.get("citation_count")
            if citation_count is not None:
                updated["citation_count"] = max(
                    int(updated.get("citation_count") or 0),
                    int(citation_count or 0),
                )
            if metrics.get("h_index") is not None:
                updated["h_index"] = metrics["h_index"]
            if metrics.get("orcid"):
                updated["orcid"] = metrics["orcid"]
            enriched.append(updated)
        return enriched

    def _enrich_researchers_with_openalex_metrics(self, researchers: list[dict]) -> list[dict]:
        if not researchers:
            return researchers
        # Avoid live API work while rebuilding the full researcher cache. Search/filter responses
        # are small enough to enrich; full-table enrichment happens lazily via profile/search.
        if len(researchers) > 500:
            return researchers

        enriched: list[dict] = []
        for item in researchers:
            author_key, _ = self._cluster_author_identity(item.get("researcher"))
            if not author_key:
                enriched.append(item)
                continue
            researcher_name = str(item.get("researcher") or "").strip().lower()
            seeded_metrics = self._seeded_openalex_metrics_for_researcher(researcher_name)
            metrics = None
            author_ids = self._openalex_author_ids_from_alias(researcher_name)
            if seeded_metrics and seeded_metrics.get("author_id"):
                seeded_author_id = self._openalex_author_id(seeded_metrics.get("author_id"))
                if seeded_author_id and seeded_author_id not in author_ids:
                    author_ids.insert(0, seeded_author_id)
            for author_id in author_ids:
                metrics = self._openalex_author_metrics(author_id)
                if metrics is not None:
                    break
            if metrics is None:
                metrics = self._openalex_author_metrics_from_search(author_key, researcher_name)
            if metrics is None:
                enriched.append(item)
                continue
            updated = dict(item)
            publication_count = metrics.get("publication_count")
            if publication_count is not None:
                updated["publication_count"] = max(
                    int(updated.get("publication_count") or 0),
                    int(publication_count or 0),
                )
                updated = self._append_openalex_unmapped_label_if_needed(
                    updated,
                    int(publication_count or 0),
                )
            citation_count = metrics.get("citation_count")
            if citation_count is not None:
                updated["citation_count"] = citation_count
            if metrics.get("h_index") is not None:
                updated["h_index"] = metrics["h_index"]
            if metrics.get("orcid"):
                updated["orcid"] = metrics["orcid"]
            enriched.append(updated)
        return enriched

    def _cluster_dashboard_rows(self, rows: list[dict]) -> list[dict]:
        clustered: dict[str, dict] = {}
        for item in rows:
            key = self._publication_cluster_key(item["work"])
            if not key:
                continue
            existing = clustered.get(key)
            if existing is None:
                clustered[key] = item
                continue
            existing_work = existing["work"]
            incoming_work = item["work"]
            existing_work["source_count"] = max(
                self._work_value(existing_work, "source_count") or 0,
                self._work_value(incoming_work, "source_count") or 0,
            )
            existing_work["citation_count"] = max(
                self._work_value(existing_work, "citation_count") or 0,
                self._work_value(incoming_work, "citation_count") or 0,
            )
            existing_work["source_names"] = list(
                {
                    *(self._work_list(existing_work, "source_names")),
                    *(self._work_list(incoming_work, "source_names")),
                }
            )
            existing_work["authors"] = list(
                {
                    *(self._work_list(existing_work, "authors")),
                    *(self._work_list(incoming_work, "authors")),
                }
            )
            existing_work["keywords"] = list(
                {
                    *(self._work_list(existing_work, "keywords")),
                    *(self._work_list(incoming_work, "keywords")),
                }
            )
        return list(clustered.values())

    def _cluster_annotated_works(self, annotated: list[dict]) -> list[dict]:
        clustered: dict[str, dict] = {}
        for item in annotated:
            key = self._publication_cluster_key(item["work"])
            if not key:
                continue
            existing = clustered.get(key)
            if existing is None:
                clustered[key] = item
                continue
            existing_work = existing["work"]
            incoming_work = item["work"]
            existing_work.source_count = max(existing_work.source_count or 0, incoming_work.source_count or 0)
            existing_work.citation_count = max(existing_work.citation_count or 0, incoming_work.citation_count or 0)
            existing_work.source_names = list({*(existing_work.source_names or []), *(incoming_work.source_names or [])})
            existing_work.authors = list({*(existing_work.authors or []), *(incoming_work.authors or [])})
            existing_work.keywords = list({*(existing_work.keywords or []), *(incoming_work.keywords or [])})
        return list(clustered.values())

    def _publication_cluster_key(self, work) -> str:
        doi = self._work_value(work, "doi")
        if doi:
            return f"doi:{str(doi).strip().lower()}"
        title = self._work_value(work, "title")
        if title:
            title_fingerprint = self._title_fingerprint(str(title))
            if title_fingerprint:
                return f"title:{title_fingerprint}"
        source_record_id = self._work_value(work, "source_record_id")
        if source_record_id:
            return f"dspace:{source_record_id}"
        return ""

    def _publication_title_cluster_key(self, work) -> str:
        title = self._work_value(work, "title")
        if not title:
            return ""
        title_fingerprint = self._title_fingerprint(str(title))
        return f"title:{title_fingerprint}" if title_fingerprint else ""

    def _work_title_matches_source_record(self, work) -> bool:
        work_id = self._work_value(work, "id")
        try:
            normalized_work_id = int(work_id)
        except (TypeError, ValueError):
            return True
        cached = self._work_source_title_match_cache.get(normalized_work_id)
        if cached is not None:
            return cached

        title_fingerprint = self._strict_title_fingerprint(self._work_value(work, "title"))
        doi = str(self._work_value(work, "doi") or "").strip().lower()
        if not title_fingerprint and not doi:
            self._work_source_title_match_cache[normalized_work_id] = True
            return True

        session = SessionLocal()
        try:
            records = session.execute(
                select(SourceRecord.title, SourceRecord.doi).where(
                    SourceRecord.work_id == normalized_work_id,
                )
            ).all()
        finally:
            session.close()

        if not records:
            self._work_source_title_match_cache[normalized_work_id] = True
            return True

        for source_title, source_doi in records:
            if doi and str(source_doi or "").strip().lower() == doi:
                self._work_source_title_match_cache[normalized_work_id] = True
                return True
            if title_fingerprint and self._strict_title_fingerprint(source_title) == title_fingerprint:
                self._work_source_title_match_cache[normalized_work_id] = True
                return True

        self._work_source_title_match_cache[normalized_work_id] = False
        return False

    def _strict_title_fingerprint(self, value: str | None) -> str:
        if not value:
            return ""
        normalized = unicodedata.normalize("NFKD", str(value))
        normalized = normalized.encode("ascii", "ignore").decode("ascii").lower()
        normalized = re.sub(r"[^a-z0-9\s]", " ", normalized)
        tokens = [token for token in normalized.split() if token]
        tokens = [
            token for token in tokens
            if token not in self._title_stopwords and not token.isdigit()
        ]
        return " ".join(tokens[:24])

    def _title_fingerprint(self, value: str | None) -> str:
        if not value:
            return ""
        # Remove subtitle segments that often differ across sources for the same work.
        base_title = re.split(r"\s*[:;\-]\s*", value, maxsplit=1)[0]
        normalized = unicodedata.normalize("NFKD", base_title)
        normalized = normalized.encode("ascii", "ignore").decode("ascii").lower()
        normalized = re.sub(r"[^a-z0-9\s]", " ", normalized)
        tokens = [token for token in normalized.split() if token]
        tokens = [
            token for token in tokens
            if token not in self._title_stopwords and not token.isdigit()
        ]
        if not tokens:
            return ""
        return " ".join(tokens[:14])

    def _cluster_author_identity(self, author: str | None) -> tuple[str, str]:
        # This is called for every author of every work â€” often multiple times for the same
        # work while building different caches â€” and academic co-authorship data means the
        # same author strings recur constantly across records. It's a pure function of
        # `author`, so memoize it: on a large harvested dataset this turns what was minutes
        # of repeated regex/unicode normalization into a dict lookup for anything but the
        # first occurrence of each distinct author string.
        if not author:
            return "", ""
        cached = self._cluster_author_identity_cache.get(author)
        if cached is not None:
            return cached
        result = self._cluster_author_identity_uncached(author)
        self._cluster_author_identity_cache[author] = result
        return result

    def _cluster_author_identity_uncached(self, author: str | None) -> tuple[str, str]:
        if not author:
            return "", ""
        cleaned = " ".join(str(author).replace(",", ", ").split())
        cleaned = re.sub(r"\b\d{4}\s*[-â€“]\s*\d{0,4}\b", "", cleaned).strip(" ,")
        cleaned = re.sub(r"\(\s*\)", "", cleaned).strip(" ,")
        alias = self._author_display_aliases.get(self._normalize_search_text(cleaned))
        if alias:
            cleaned = alias
        parenthetical_name = re.match(
            r"^[A-Za-z](?:\s*\.\s*[A-Za-z])*\.?\s*\((?P<given>[^)]+)\)\s*,?\s*(?:\d{4}\s+)?(?P<surname>[A-Za-z][A-Za-z' -]+)$",
            cleaned,
        )
        if parenthetical_name:
            given = " ".join(parenthetical_name.group("given").split())
            surname = " ".join(parenthetical_name.group("surname").split())
            cleaned = f"{given} {surname}".strip()
        if not cleaned:
            return "", ""
        lower_cleaned = cleaned.lower()
        if "@" in lower_cleaned:
            return "", ""
        if self._is_likely_non_person_author(cleaned):
            return "", ""
        if any(
            bad in lower_cleaned
            for bad in [
                "university",
                "department",
                "departement",
                "faculty",
                "school of",
                "institute",
                "college",
                "consortium",
                "committee",
                "daily sketch",
                "newspaper",
                "publieke werke",
                "public works",
                "society",
                "workshop",
                "conference",
                "symposium",
                "seminar",
                "group",
                "team",
                "national park",
                "anonymous",
                "unknown",
                "et al",
            ]
        ):
            return "", ""
        normalized = unicodedata.normalize("NFKD", cleaned).encode("ascii", "ignore").decode("ascii").lower()
        normalized = re.sub(r"[^a-z,\s-]", " ", normalized)
        normalized = " ".join(normalized.split())
        if not normalized:
            return "", cleaned
        if "," in normalized:
            last_name, given_names = [part.strip() for part in normalized.split(",", 1)]
            given_tokens = [token for token in given_names.split(" ") if token]
            surname = last_name
        else:
            tokens = [token for token in normalized.split(" ") if token]
            if not tokens:
                return "", cleaned
            surname = tokens[-1]
            given_tokens = tokens[:-1]
        all_tokens = [*given_tokens, surname]
        if len(all_tokens) < 2 or len(all_tokens) > 6:
            return "", cleaned
        if any(len(token) == 0 for token in all_tokens):
            return "", cleaned
        if any(token.isdigit() for token in all_tokens):
            return "", cleaned
        given_signature = self._author_given_signature(given_tokens)
        if not given_signature:
            return "", cleaned
        return f"{surname}|{given_signature}", cleaned

    def _author_given_signature(self, given_tokens: list[str]) -> str:
        signature_parts: list[str] = []
        for index, token in enumerate(given_tokens):
            token = token.strip()
            if not token:
                continue
            if index == 0 and len(token) > 1:
                signature_parts.append(token)
            else:
                signature_parts.append(token[0])
        return "".join(signature_parts)

    def _is_likely_non_person_author(self, author: str) -> bool:
        cleaned = " ".join(str(author or "").split()).strip()
        if not cleaned:
            return True
        lower_cleaned = cleaned.lower()
        if ":" in cleaned:
            return True
        if re.search(r"\b\d{4}\s*[-Ã¢â‚¬â€œ]\s*\d{0,4}\b", cleaned):
            without_dates = re.sub(r"\b\d{4}\s*[-Ã¢â‚¬â€œ]\s*\d{0,4}\b", "", cleaned).strip(" ,()")
            if "," not in without_dates and len(without_dates.split()) <= 3:
                return True
        non_person_phrases = (
            "high court",
            "court of",
            "daily sketch",
            "departement",
            "dept.",
            "conference",
            "congress",
            "colloquium",
            "workshop",
            "symposium",
            "seminar",
            "national park",
            "newspaper",
            "publieke werke",
            "public works",
            "society",
        )
        if any(phrase in lower_cleaned for phrase in non_person_phrases):
            return True
        return False

    def _iter_author_entries(self, authors: list[str] | None) -> list[str]:
        entries: list[str] = []
        for raw in authors or []:
            if raw is None:
                continue
            text = " ".join(str(raw).split()).strip()
            if not text:
                continue
            parts = re.split(r"\s*(?:;|\||\band\b|&)\s*", text, flags=re.IGNORECASE)
            for part in parts:
                cleaned = " ".join(part.split()).strip(" ,;|")
                if cleaned:
                    entries.append(cleaned)
        return entries

    def _pick_better_author_name(self, existing: str, candidate: str) -> str:
        if not existing:
            return candidate
        if not candidate:
            return existing
        existing_tokens = [token.strip(".") for token in existing.split() if token]
        candidate_tokens = [token.strip(".") for token in candidate.split() if token]
        existing_score = (
            sum(1 for token in existing_tokens if len(token) > 1),
            sum(len(token) for token in existing_tokens),
        )
        candidate_score = (
            sum(1 for token in candidate_tokens if len(token) > 1),
            sum(len(token) for token in candidate_tokens),
        )
        return candidate if candidate_score > existing_score else existing

    def _dspace_values(self, payload: dict, key: str) -> list[str]:
        value = payload.get(key) if isinstance(payload, dict) else None
        if value is None:
            return []
        values = value if isinstance(value, list) else [value]
        return [" ".join(str(item).split()) for item in values if str(item).strip()]

    def _first_dspace_value(self, payload: dict, key: str) -> str | None:
        values = self._dspace_values(payload, key)
        return values[0] if values else None

    def _dspace_creators(self, payload: dict) -> list[str]:
        return self._iter_author_entries(self._dspace_values(payload, "creator"))

    def _main_author_home_by_key(self, dspace_rows: list[tuple[Work, SourceRecord]]) -> dict[str, tuple[str, str]]:
        scores: dict[str, dict[tuple[str, str], int]] = {}
        for work, dspace_record in dspace_rows:
            dspace_payload = dspace_record.raw_payload or {}
            dspace_authors = self._dspace_creators(dspace_payload)
            main_author = dspace_authors[0] if dspace_authors else None
            author_key, _ = self._cluster_author_identity(main_author)
            if not author_key:
                continue

            title = dspace_record.title or self._first_dspace_value(dspace_payload, "title") or work.title
            faculty, department = self._dspace_faculty_department(
                dspace_payload=dspace_payload,
                title=title,
                journal=work.journal,
                publisher=work.publisher,
                keywords=work.keywords or [],
            )
            if not self._visible_mapping_label(faculty) and not self._visible_mapping_label(department):
                continue
            scores.setdefault(author_key, {})
            scores[author_key][(faculty, department)] = scores[author_key].get((faculty, department), 0) + 1

        return {
            author_key: sorted(
                faculty_department_counts,
                key=lambda item: (
                    -faculty_department_counts[item],
                    not self._visible_mapping_label(item[0]),
                    not self._visible_mapping_label(item[1]),
                    item[0],
                    item[1],
                ),
            )[0]
            for author_key, faculty_department_counts in scores.items()
        }

    def _main_author_faculty_department(
        self,
        *,
        main_author: str | None,
        main_author_home_by_key: dict[str, tuple[str, str]],
        dspace_payload: dict,
        title: str | None,
        journal: str | None,
        publisher: str | None,
        keywords: list[str],
    ) -> tuple[str, str]:
        author_key, _ = self._cluster_author_identity(main_author)
        if author_key and author_key in main_author_home_by_key:
            return main_author_home_by_key[author_key]

        return self._dspace_faculty_department(
            dspace_payload=dspace_payload,
            title=title,
            journal=journal,
            publisher=publisher,
            keywords=keywords,
        )

    def _dspace_faculty_department(
        self,
        *,
        dspace_payload: dict,
        title: str | None,
        journal: str | None,
        publisher: str | None,
        keywords: list[str],
    ) -> tuple[str, str]:
        faculty = self._extract_metadata_label(
            dspace_payload,
            ["faculty", "dc.contributor.faculty", "contributorfaculty"],
        )
        department = self._extract_metadata_label(
            dspace_payload,
            ["department", "dc.contributor.department", "contributordepartment"],
        )
        if faculty and department:
            return faculty, department

        labelled_faculty, labelled_department = self._extract_exact_faculty_department_label(
            [
                *self._dspace_values(dspace_payload, "subject"),
                *self._dspace_values(dspace_payload, "description"),
            ]
        )
        if labelled_faculty or labelled_department:
            return faculty or labelled_faculty or "Unmapped", department or labelled_department or "Unmapped"

        inferred_faculty, inferred_department = classify_record(
            title=title,
            journal=journal,
            publisher=publisher,
            keywords=[*keywords, *self._dspace_values(dspace_payload, "subject")],
            abstract=self._first_dspace_value(dspace_payload, "description"),
        )
        return faculty or inferred_faculty, department or inferred_department

    def _extract_exact_faculty_department_label(self, values: list[str]) -> tuple[str | None, str | None]:
        faculty_lookup = {faculty.lower(): faculty for faculty in faculty_options()}
        department_lookup = {
            department.lower(): (faculty, department)
            for faculty, departments in DEPARTMENT_RULES.items()
            for department, _ in departments
        }

        for raw_value in values:
            value = " ".join(str(raw_value or "").split()).strip()
            if not value or len(value) > 80:
                continue
            normalized = value.lower()
            if normalized in department_lookup:
                return department_lookup[normalized]
            if normalized in faculty_lookup:
                return faculty_lookup[normalized], None
        return None, None

    def _extract_metadata_label(self, payload: dict, candidate_keys: list[str]) -> str | None:
        normalized_candidates = {self._metadata_key(key) for key in candidate_keys}
        for key, value in (payload or {}).items():
            if self._metadata_key(str(key)) not in normalized_candidates:
                continue
            values = value if isinstance(value, list) else [value]
            for item in values:
                text = " ".join(str(item).split()).strip()
                if text:
                    return text
        return None

    def _metadata_key(self, value: str) -> str:
        return re.sub(r"[^a-z]", "", value.lower())

    def _openalex_institutions(self, source_records: list[SourceRecord]) -> list[str]:
        institutions: list[str] = []
        seen: set[str] = set()
        for record in source_records:
            if record.source_name != "openalex" or not isinstance(record.raw_payload, dict):
                continue
            for authorship in record.raw_payload.get("authorships", []):
                for institution in authorship.get("institutions", []):
                    name = institution.get("display_name")
                    if not name:
                        continue
                    key = name.strip().lower()
                    if key not in seen:
                        institutions.append(name.strip())
                        seen.add(key)
        return institutions

    def _researcher_orcid(self, target_key: str, target_raw: str, researcher_rows: list[dict]) -> str | None:
        if not target_key and not target_raw:
            return None
        cache_key = target_key or target_raw
        if cache_key in self._researcher_orcid_cache:
            return self._researcher_orcid_cache[cache_key]

        work_ids: list[int] = []
        seen_work_ids: set[int] = set()
        for item in researcher_rows:
            work_id = (item.get("work") or {}).get("id")
            try:
                normalized_work_id = int(work_id)
            except (TypeError, ValueError):
                continue
            if normalized_work_id in seen_work_ids:
                continue
            seen_work_ids.add(normalized_work_id)
            work_ids.append(normalized_work_id)

        if not work_ids:
            self._refresh_researcher_orcid_async(cache_key, target_key, target_raw)
            return self._researcher_orcid_cache.get(cache_key)

        session = SessionLocal()
        openalex_author_ids: list[str] = []
        try:
            for start in range(0, len(work_ids), 500):
                batch = work_ids[start:start + 500]
                records = session.execute(
                    select(SourceRecord).where(
                        SourceRecord.work_id.in_(batch),
                        SourceRecord.source_name == "openalex",
                    )
                ).scalars().all()
                for record in records:
                    orcid, author_ids = self._orcid_from_openalex_payload(record.raw_payload, target_key, target_raw)
                    if orcid:
                        self._researcher_orcid_cache[cache_key] = orcid
                        return orcid
                    for author_id in author_ids:
                        if author_id not in openalex_author_ids:
                            openalex_author_ids.append(author_id)
        finally:
            session.close()
        for author_id in openalex_author_ids:
            orcid = self._orcid_from_openalex_author_id(author_id)
            if orcid:
                self._researcher_orcid_cache[cache_key] = orcid
                return orcid
        orcid = self._orcid_from_openalex_author_search(target_key, target_raw)
        self._researcher_orcid_cache[cache_key] = orcid
        return orcid

    def _researcher_openalex_metrics(
        self,
        target_key: str,
        target_raw: str,
        researcher_rows: list[dict],
        allow_live: bool = True,
    ) -> dict | None:
        seeded_metrics = self._seeded_openalex_metrics_for_researcher(target_raw or target_key)
        has_openalex_rows = any(
            any(str(source or "").strip().lower() == "openalex" for source in ((item.get("work") or {}).get("source_names") or []))
            for item in researcher_rows
        )
        if not has_openalex_rows:
            cache_key = f"search:{target_key or target_raw}"
            cached_search_metrics = self._openalex_author_metrics_cache.get(cache_key)
            if cached_search_metrics is not None:
                return cached_search_metrics
            if seeded_metrics and seeded_metrics.get("author_id"):
                cached_id_metrics = self._openalex_author_metrics_cache.get(
                    self._openalex_author_id(seeded_metrics.get("author_id")) or ""
                )
                if cached_id_metrics is not None:
                    return cached_id_metrics
            return None
        author_ids = self._openalex_author_ids_for_researcher(target_key, target_raw, researcher_rows)
        if seeded_metrics and seeded_metrics.get("author_id"):
            seeded_author_id = self._openalex_author_id(seeded_metrics.get("author_id"))
            if seeded_author_id and seeded_author_id not in author_ids:
                author_ids.insert(0, seeded_author_id)
        if not author_ids:
            author_ids = self._openalex_author_ids_from_alias(target_raw)
        for author_id in author_ids:
            normalized_author_id = self._openalex_author_id(author_id)
            cached_metrics = self._openalex_author_metrics_cache.get(normalized_author_id)
            if cached_metrics is not None:
                return cached_metrics
            if not allow_live:
                continue
            metrics = self._openalex_author_metrics(author_id)
            if metrics is not None:
                return metrics
        if not allow_live:
            return self._openalex_author_metrics_cache.get(f"search:{target_key or target_raw}")
        return self._openalex_author_metrics_from_search(target_key, target_raw)

    def _openalex_author_publication_rows(self, author_id: str | None, allow_live: bool = True) -> list[dict]:
        normalized_author_id = self._openalex_author_id(author_id or "")
        if not normalized_author_id:
            return []
        now = time.monotonic()
        cached = self._openalex_author_works_cache.get(normalized_author_id)
        if cached is not None and now < cached[0]:
            return cached[1]
        return self._openalex_author_publication_rows_from_source_records(normalized_author_id)

    def _metadata_author_name_matches_profile(self, author_name: str, target_raw: str) -> bool:
        normalized_author = self._normalize_search_text(author_name)
        normalized_target = self._normalize_search_text(target_raw)
        if not normalized_author or not normalized_target:
            return False
        if normalized_author == normalized_target:
            return True
        author_parts = [part.strip() for part in str(author_name or "").split(",", 1)]
        if len(author_parts) == 2 and all(author_parts):
            normalized_parts = [self._normalize_search_text(part) for part in author_parts]
            return sorted(normalized_parts) == sorted(normalized_target.split())
        return False

    def _openalex_author_publication_rows_for_title(
        self,
        author_id: str | None,
        publication_title_query: str,
    ) -> list[dict]:
        normalized_author_id = self._openalex_author_id(author_id or "")
        if not normalized_author_id or not publication_title_query:
            return []

        stored_rows = [
            row
            for row in self._openalex_author_publication_rows_from_source_records(normalized_author_id)
            if publication_title_query in self._normalize_search_text(row.get("title"))
        ]
        if stored_rows:
            return stored_rows

        now = time.monotonic()
        cached = self._openalex_author_works_cache.get(normalized_author_id)
        if cached is not None and now < cached[0]:
            return [
                row
                for row in cached[1]
                if publication_title_query in self._normalize_search_text(row.get("title"))
            ]

        return []

    def _openalex_work_publication_row(self, work: dict) -> dict:
        open_access = work.get("open_access") or {}
        primary_location = work.get("primary_location") or {}
        source = primary_location.get("source") or {}
        uri = work.get("id") or work.get("doi") or primary_location.get("landing_page_url")
        return {
            "title": work.get("display_name") or "Untitled publication",
            "year": self._normalize_publication_year(work.get("publication_year")),
            "source": source.get("display_name") or "OpenAlex",
            "citations": work.get("cited_by_count") or 0,
            "open_access_status": open_access.get("oa_status") or "unknown",
            "uri": uri,
            "url": uri,
        }

    def _openalex_author_publication_rows_from_source_records(self, author_id: str) -> list[dict]:
        session = SessionLocal()
        try:
            records = session.execute(
                select(SourceRecord).where(
                    SourceRecord.source_name == "openalex",
                    cast(SourceRecord.raw_payload, String).like(f"%{author_id}%"),
                )
            ).scalars().all()
        finally:
            session.close()

        rows: list[dict] = []
        for record in records:
            payload = record.raw_payload or {}
            open_access = payload.get("open_access") or {}
            primary_location = payload.get("primary_location") or {}
            source = primary_location.get("source") or {}
            uri = payload.get("id") or payload.get("doi") or primary_location.get("landing_page_url") or record.source_url
            rows.append(
                {
                    "title": payload.get("display_name") or record.title or "Untitled publication",
                    "year": self._normalize_publication_year(
                        payload.get("publication_year") or record.publication_year
                    ),
                    "source": source.get("display_name") or "OpenAlex",
                    "citations": payload.get("cited_by_count") or record.citation_count or 0,
                    "open_access_status": open_access.get("oa_status") or "unknown",
                    "uri": uri,
                    "url": uri,
                }
            )
        rows.sort(
            key=lambda row: (row["year"] if row["year"] is not None else -1, row["citations"]),
            reverse=True,
        )
        self._openalex_author_works_cache[author_id] = (
            time.monotonic() + self._researchers_cache_ttl_seconds,
            rows,
        )
        return rows

    def _openalex_author_metrics_from_source_records(self, author_id: str) -> dict | None:
        normalized_author_id = self._openalex_author_id(author_id)
        if not normalized_author_id:
            return None
        rows = self._openalex_author_publication_rows_from_source_records(normalized_author_id)
        if not rows:
            return None
        citation_counts = sorted((int(row.get("citations") or 0) for row in rows), reverse=True)
        h_index = 0
        for index, citation_count in enumerate(citation_counts, start=1):
            if citation_count >= index:
                h_index = index
            else:
                break
        return {
            "author_id": normalized_author_id,
            "publication_count": len(rows),
            "citation_count": sum(citation_counts),
            "h_index": h_index,
            "orcid": self._orcid_from_openalex_source_records(normalized_author_id),
        }

    def _orcid_from_openalex_source_records(self, author_id: str) -> str | None:
        normalized_author_id = self._openalex_author_id(author_id)
        if not normalized_author_id:
            return None
        session = SessionLocal()
        try:
            records = session.execute(
                select(SourceRecord).where(
                    SourceRecord.source_name == "openalex",
                    cast(SourceRecord.raw_payload, String).like(f"%{normalized_author_id}%"),
                )
            ).scalars().all()
        finally:
            session.close()
        for record in records:
            payload = record.raw_payload if isinstance(record.raw_payload, dict) else {}
            for authorship in payload.get("authorships") or []:
                author = authorship.get("author") or {}
                if self._openalex_author_id(author.get("id")) != normalized_author_id:
                    continue
                orcid = self._normalize_orcid(author.get("orcid"))
                if orcid:
                    return orcid
        return None

    def _openalex_author_ids_for_researcher(
        self,
        target_key: str,
        target_raw: str,
        researcher_rows: list[dict],
    ) -> list[str]:
        work_ids: list[int] = []
        seen_work_ids: set[int] = set()
        for item in researcher_rows:
            work_id = (item.get("work") or {}).get("id")
            try:
                normalized_work_id = int(work_id)
            except (TypeError, ValueError):
                continue
            if normalized_work_id in seen_work_ids:
                continue
            seen_work_ids.add(normalized_work_id)
            work_ids.append(normalized_work_id)

        if not work_ids:
            return []

        author_ids: list[str] = []
        session = SessionLocal()
        try:
            for start in range(0, len(work_ids), 500):
                batch = work_ids[start:start + 500]
                records = session.execute(
                    select(SourceRecord).where(
                        SourceRecord.work_id.in_(batch),
                        SourceRecord.source_name == "openalex",
                    )
                ).scalars().all()
                for record in records:
                    _, payload_author_ids = self._orcid_from_openalex_payload(
                        record.raw_payload,
                        target_key,
                        target_raw,
                    )
                    for author_id in payload_author_ids:
                        if author_id not in author_ids:
                            author_ids.append(author_id)
        finally:
            session.close()
        return author_ids

    def _openalex_author_ids_from_alias(self, target_raw: str) -> list[str]:
        query = self._openalex_author_search_query(target_raw)
        if not query:
            return []
        query_normalized = self._normalize_search_text(query)
        target_key, _ = self._cluster_author_identity(query)
        target_surname = target_key.split("|", 1)[0] if "|" in target_key else ""
        session = SessionLocal()
        try:
            aliases = session.execute(
                select(AuthorAlias.identity_key, AuthorAlias.canonical_name).where(
                    AuthorAlias.source_name == "openalex",
                    (
                        AuthorAlias.canonical_name == query
                        if not target_surname
                        else (
                            (AuthorAlias.canonical_name == query)
                            | AuthorAlias.canonical_name.ilike(f"%{target_surname}%")
                            | AuthorAlias.identity_key.ilike(f"fingerprint:{target_surname}|%")
                        )
                    ),
                )
            ).all()
        finally:
            session.close()

        author_ids: list[str] = []
        matched_canonical_names: set[str] = set()
        for identity_key, canonical_name in aliases:
            identity_key_text = str(identity_key or "")
            canonical_key, _ = self._cluster_author_identity(canonical_name)
            identity_fingerprint = (
                identity_key_text.split(":", 1)[1]
                if identity_key_text.startswith("fingerprint:")
                else ""
            )
            canonical_normalized = self._normalize_search_text(canonical_name)
            is_match = (
                canonical_normalized == query_normalized
                or self._author_keys_compatible(canonical_key, target_key)
                or self._author_keys_compatible(identity_fingerprint, target_key)
            )
            if not is_match:
                continue
            if canonical_name:
                matched_canonical_names.add(canonical_name)
            if not identity_key_text.startswith("id:"):
                continue
            author_id = self._openalex_author_id(identity_key_text.split(":", 1)[1])
            if author_id and author_id not in author_ids:
                author_ids.append(author_id)

        if matched_canonical_names:
            session = SessionLocal()
            try:
                id_aliases = session.execute(
                    select(AuthorAlias.identity_key).where(
                        AuthorAlias.source_name == "openalex",
                        AuthorAlias.canonical_name.in_(matched_canonical_names),
                        AuthorAlias.identity_key.like("id:%"),
                    )
                ).scalars().all()
            finally:
                session.close()
            for identity_key in id_aliases:
                author_id = self._openalex_author_id(str(identity_key).split(":", 1)[1])
                if author_id and author_id not in author_ids:
                    author_ids.append(author_id)
        return author_ids

    def _author_keys_compatible(self, left_key: str | None, right_key: str | None) -> bool:
        if not left_key or not right_key or "|" not in left_key or "|" not in right_key:
            return False
        left_surname, left_signature = left_key.split("|", 1)
        right_surname, right_signature = right_key.split("|", 1)
        if left_surname != right_surname or not left_signature or not right_signature:
            return False
        if left_signature == right_signature:
            return True
        if len(left_signature) >= 3 and right_signature.startswith(left_signature):
            return True
        if len(right_signature) >= 3 and left_signature.startswith(right_signature):
            return True
        left_initials = {left_signature}
        right_initials = {right_signature}
        if len(left_signature) > 1:
            left_initials.add(left_signature[0] + left_signature[-1])
        if len(right_signature) > 1:
            right_initials.add(right_signature[0] + right_signature[-1])
        return bool(left_initials & right_initials)

    def _openalex_author_metrics(self, author_id: str) -> dict | None:
        normalized_author_id = self._openalex_author_id(author_id)
        if not normalized_author_id:
            return None
        cached_metrics = self._openalex_author_metrics_cache.get(normalized_author_id)
        if cached_metrics is not None:
            return cached_metrics
        metrics = self._openalex_author_metrics_from_source_records(normalized_author_id)
        if metrics is None:
            return None
        self._openalex_author_metrics_cache[normalized_author_id] = metrics
        return metrics

    def _openalex_author_metrics_from_search(self, target_key: str, target_raw: str) -> dict | None:
        query = self._openalex_author_search_query(target_raw)
        if not query:
            return None
        cache_key = f"search:{target_key or ''}:{self._normalize_search_text(query)}"
        cached_metrics = self._openalex_author_metrics_cache.get(cache_key)
        if cached_metrics is not None:
            return cached_metrics

        for author_id in self._openalex_author_ids_from_alias(target_raw):
            metrics = self._openalex_author_metrics(author_id)
            if metrics is not None:
                self._openalex_author_metrics_cache[cache_key] = metrics
                return metrics
        return None

    def _refresh_researcher_orcid_async(self, cache_key: str, target_key: str, target_raw: str) -> None:
        if cache_key in self._researcher_orcid_cache:
            return
        pending_key = f"pending:{cache_key}"
        if pending_key in self._openalex_author_orcid_cache:
            return
        self._openalex_author_orcid_cache[pending_key] = None

        def refresh() -> None:
            try:
                self._researcher_orcid_cache[cache_key] = self._orcid_from_openalex_author_search(
                    target_key,
                    target_raw,
                )
            finally:
                self._openalex_author_orcid_cache.pop(pending_key, None)

        threading.Thread(target=refresh, daemon=True).start()

    def _orcid_from_openalex_payload(
        self,
        payload: dict | None,
        target_key: str,
        target_raw: str,
    ) -> tuple[str | None, list[str]]:
        author_ids: list[str] = []
        if not isinstance(payload, dict):
            return None, author_ids
        for authorship in payload.get("authorships") or []:
            author = authorship.get("author") or {}
            author_key, author_display = self._cluster_author_identity(author.get("display_name"))
            if target_key and not self._author_keys_compatible(author_key, target_key):
                continue
            if not target_key and self._normalize_search_text(author_display) != self._normalize_search_text(target_raw):
                continue
            author_id = self._openalex_author_id(author.get("id"))
            if author_id and author_id not in author_ids:
                author_ids.append(author_id)
            orcid = self._normalize_orcid(author.get("orcid"))
            if orcid:
                return orcid, author_ids
        return None, author_ids

    def _orcid_from_openalex_author_id(self, author_id: str) -> str | None:
        normalized_author_id = self._openalex_author_id(author_id)
        if not normalized_author_id:
            return None
        if normalized_author_id in self._openalex_author_orcid_cache:
            return self._openalex_author_orcid_cache[normalized_author_id]
        orcid = self._orcid_from_openalex_source_records(normalized_author_id)
        self._openalex_author_orcid_cache[normalized_author_id] = orcid
        return orcid

    def _orcid_from_openalex_author_search(self, target_key: str, target_raw: str) -> str | None:
        query = self._openalex_author_search_query(target_raw)
        if not query:
            return None
        cache_key = f"search:{target_key or ''}:{self._normalize_search_text(query)}"
        if cache_key in self._openalex_author_orcid_cache:
            return self._openalex_author_orcid_cache[cache_key]
        alias_orcid = self._orcid_from_openalex_alias(target_raw)
        if alias_orcid:
            self._openalex_author_orcid_cache[cache_key] = alias_orcid
            return alias_orcid
        self._openalex_author_orcid_cache[cache_key] = None
        return None

    def _orcid_from_openalex_alias(self, target_raw: str) -> str | None:
        query = self._openalex_author_search_query(target_raw)
        if not query:
            return None
        query_normalized = self._normalize_search_text(query)
        target_key, _ = self._cluster_author_identity(query)
        target_surname = target_key.split("|", 1)[0] if "|" in target_key else ""
        session = SessionLocal()
        try:
            aliases = session.execute(
                select(AuthorAlias.identity_key, AuthorAlias.canonical_name).where(
                    AuthorAlias.source_name == "openalex",
                    AuthorAlias.identity_key.like("orcid:%"),
                    (
                        AuthorAlias.canonical_name == query
                        if not target_surname
                        else (
                            (AuthorAlias.canonical_name == query)
                            | AuthorAlias.canonical_name.ilike(f"%{target_surname}%")
                        )
                    ),
                )
            ).all()
        finally:
            session.close()
        for identity_key, canonical_name in aliases:
            canonical_key, _ = self._cluster_author_identity(canonical_name)
            canonical_normalized = self._normalize_search_text(canonical_name)
            if (
                canonical_normalized == query_normalized
                or self._author_keys_compatible(canonical_key, target_key)
            ):
                return self._normalize_orcid(str(identity_key).split(":", 1)[1])
        return None

    def _openalex_author_search_query(self, target_raw: str) -> str:
        cleaned = " ".join(str(target_raw or "").split()).strip(" ,")
        if not cleaned:
            return ""
        if "," in cleaned:
            surname, given = [part.strip() for part in cleaned.split(",", 1)]
            return " ".join(part for part in (given, surname) if part)
        return cleaned

    def _openalex_author_id(self, value: str | None) -> str | None:
        if not value:
            return None
        text = str(value).strip()
        if not text:
            return None
        match = re.search(r"A\d+", text, flags=re.IGNORECASE)
        return match.group(0).upper() if match else None

    def _normalize_orcid(self, value: str | None) -> str | None:
        if not value:
            return None
        cleaned = str(value).strip()
        if not cleaned:
            return None
        if cleaned.lower().startswith("https://orcid.org/"):
            return cleaned
        orcid_match = re.search(r"\d{4}-\d{4}-\d{4}-[\dXx]{4}", cleaned)
        if not orcid_match:
            return cleaned
        return f"https://orcid.org/{orcid_match.group(0).upper()}"

    def _openalex_institutions_by_work_id(
            self,
            session,
            work_ids: list,
        ) -> dict:
        if not work_ids:
            return {}

        openalex_records = session.execute(
            select(SourceRecord).where(
                SourceRecord.work_id.in_(work_ids),
                SourceRecord.source_name == "openalex",
            )
        ).scalars().all()

        result: dict = {}
        for record in openalex_records:
            if not isinstance(record.raw_payload, dict):
                continue
            seen: set[str] = set()
            institutions: list[str] = []
            for authorship in record.raw_payload.get("authorships", []):
                for institution in authorship.get("institutions", []):
                    name = institution.get("display_name")
                    if not name:
                        continue
                    key = name.strip().lower()
                    if key not in seen:
                        institutions.append(name.strip())
                        seen.add(key)
            if institutions:
                result.setdefault(record.work_id, [])
                for inst in institutions:
                    if inst.lower() not in {i.lower() for i in result[record.work_id]}:
                        result[record.work_id].append(inst)

        return result

    def _dspace_baseline_counts(self) -> dict:
        now = time.monotonic()
        if self._dspace_baseline_cache is not None and now < self._dspace_baseline_cache[0]:
            return self._dspace_baseline_cache[1]

        session = SessionLocal()
        try:
            records = session.execute(
                select(SourceRecord.source_record_id, SourceRecord.raw_payload).where(
                    SourceRecord.source_name == "dspace"
                )
            ).all()
        finally:
            session.close()

        publication_ids: set[str] = set()
        researcher_ids: set[str] = set()
        for record in records:
            if record.source_record_id:
                publication_ids.add(str(record.source_record_id))
            payload = record.raw_payload or {}
            creators = payload.get("creator") if isinstance(payload, dict) else []
            if not isinstance(creators, list):
                creators = [creators]
            creator_entries = self._iter_author_entries(creators)
            # Use primary creator only for institutional headcount-style researcher totals.
            primary_creator = creator_entries[0] if creator_entries else None
            author_key, _ = self._cluster_author_identity(primary_creator)
            if author_key:
                researcher_ids.add(author_key)

        baseline = {
            "total_publications": len(publication_ids),
            "researcher_count": len(researcher_ids),
        }
        self._dspace_baseline_cache = (now + self._works_cache_ttl_seconds, baseline)
        return baseline

    def _openalex_institution_author_count(self) -> int | None:
        now = time.monotonic()
        cached = self._openalex_institution_author_count_cache
        if cached is not None and now < cached[0]:
            return cached[1]
        normalized_count = self._default_institution_researcher_count
        self._openalex_institution_author_count_cache = (
            now + self._researchers_cache_ttl_seconds,
            normalized_count,
        )
        return normalized_count

    def _rank_counts(self, counts: dict, key_name: str, limit: int | None = None) -> list[dict]:
        ranked = sorted(counts.items(), key=lambda item: (-item[1], str(item[0]).lower()))
        if limit is not None:
            ranked = ranked[:limit]
        return [{key_name: label, "count": count} for label, count in ranked]

    def _serialize_run(self, run: HarvestRun) -> dict:
        return {
            "id": run.id,
            "source_name": run.source_name,
            "status": run.status,
            "records_fetched": run.records_fetched,
            "records_upserted": run.records_upserted,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "error_message": run.error_message,
        }

    def _calculate_h_index(self, annotated: list[dict]) -> int:
        citation_counts = sorted(
            (item["work"].citation_count or 0 for item in annotated),
            reverse=True,
        )
        h_index = 0
        for index, citation_count in enumerate(citation_counts, start=1):
            if citation_count >= index:
                h_index = index
            else:
                break
        return h_index

    def _is_collaborative(self, work: Work) -> bool:
        affiliations = {
            affiliation.strip().lower()
            for affiliation in (work.affiliations or [])
            if isinstance(affiliation, str) and affiliation.strip()
        }
        if len(affiliations) > 1:
            return True
        return len(work.authors or []) > 1

    def _is_collaborative_dashboard_work(self, work: dict) -> bool:
        affiliations = {
            affiliation.strip().lower()
            for affiliation in self._work_list(work, "affiliations")
            if isinstance(affiliation, str) and affiliation.strip()
        }
        if len(affiliations) > 1:
            return True
        return len(self._work_list(work, "authors")) > 1

    def _work_value(self, work: Work | dict, key: str):
        if isinstance(work, dict):
            return work.get(key)
        return getattr(work, key, None)

    def _work_list(self, work: Work | dict, key: str) -> list:
        value = self._work_value(work, key)
        return value or []

    def _main_and_affiliate_institutions(self, institutions: list) -> tuple[str | None, list[str]]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for institution in institutions or []:
            label = " ".join(str(institution or "").split()).strip()
            key = label.lower()
            if not label or key in seen:
                continue
            cleaned.append(label)
            seen.add(key)

        if not cleaned:
            return None, []

        preferred_index = next(
            (
                index
                for index, institution in enumerate(cleaned)
                if "university of pretoria" in institution.lower()
            ),
            0,
        )
        main_institution = cleaned[preferred_index]
        affiliate_institutions = [
            institution
            for index, institution in enumerate(cleaned)
            if index != preferred_index
        ]
        return main_institution, affiliate_institutions

    def _normalize_publication_years(
        self,
        publication_years: list[int] | tuple[int, ...] | None,
    ) -> tuple[int, ...]:
        normalized = {
            year
            for year in (
                self._normalize_publication_year(raw_year)
                for raw_year in (publication_years or [])
            )
            if year is not None
        }
        return tuple(sorted(normalized))

    def _normalize_publication_year(self, value) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _publication_url(self, work: Work | dict) -> str | None:
        source_url = self._work_value(work, "primary_source_url")
        valid_source_url = self._first_url([source_url])
        if valid_source_url:
            return valid_source_url

        doi = self._work_value(work, "doi")
        if doi:
            return f"https://doi.org/{doi}"

        return None

    def _profile_publication_uri(self, work: Work | dict) -> str | None:
        existing_uri = self._first_url(
            [
                self._work_value(work, "uri"),
                self._dspace_uri_from_work(work),
            ]
        )
        if existing_uri:
            return existing_uri

        metadata_uri = self._article_metadata_uri_for_work(work)
        if metadata_uri:
            return metadata_uri

        work_id = self._work_value(work, "id")
        try:
            normalized_work_id = int(work_id)
        except (TypeError, ValueError):
            normalized_work_id = 0
        if normalized_work_id:
            if normalized_work_id in self._profile_publication_uri_cache:
                return self._profile_publication_uri_cache[normalized_work_id]
            uri = self._profile_publication_uri_from_source_records(normalized_work_id, work)
            self._profile_publication_uri_cache[normalized_work_id] = uri
            if uri:
                return uri

        doi = self._work_value(work, "doi")
        if doi:
            return f"https://doi.org/{doi}"
        return self._publication_url(work)

    def _article_metadata_uri_for_work(self, work: Work | dict) -> str | None:
        lookup = self._article_metadata_uri_lookup()
        doi = self._normalize_doi_key(self._work_value(work, "doi"))
        if doi:
            uri = lookup.get(f"doi:{doi}")
            if uri:
                return uri
        title_key = self._title_fingerprint(self._work_value(work, "title"))
        if title_key:
            return lookup.get(f"title:{title_key}")
        return None

    def _article_metadata_uri_lookup(self) -> dict[str, str]:
        now = time.monotonic()
        cached = self._article_metadata_uri_lookup_cache
        if cached is not None and now < cached[0]:
            return cached[1]

        lookup: dict[str, str] = {}
        for record in self._article_metadata_records():
            uri = self._first_url([record.get("Article URI")])
            if not uri:
                continue
            doi = self._normalize_doi_key(record.get("DOI"))
            if doi:
                lookup.setdefault(f"doi:{doi}", uri)
            title_key = self._title_fingerprint(record.get("Article Title"))
            if title_key:
                lookup.setdefault(f"title:{title_key}", uri)

        self._article_metadata_uri_lookup_cache = (
            now + self._dashboard_cache_ttl_seconds,
            lookup,
        )
        return lookup

    def _profile_publication_uris(self, works: list[Work | dict]) -> dict[int, str | None]:
        result: dict[int, str | None] = {}
        unresolved_by_work_id: dict[int, list[Work | dict]] = {}
        for work in works:
            result_key = id(work)
            existing_uri = self._first_url(
                [
                    self._work_value(work, "uri"),
                    self._dspace_uri_from_work(work),
                ]
            )
            if existing_uri:
                result[result_key] = existing_uri
                continue
            metadata_uri = self._article_metadata_uri_for_work(work)
            if metadata_uri:
                result[result_key] = metadata_uri
                continue
            work_id = self._work_value(work, "id")
            try:
                normalized_work_id = int(work_id)
            except (TypeError, ValueError):
                normalized_work_id = 0
            if normalized_work_id and normalized_work_id in self._profile_publication_uri_cache:
                result[result_key] = self._profile_publication_uri_cache[normalized_work_id]
                continue
            if normalized_work_id:
                uri = self._publication_url(work)
                self._profile_publication_uri_cache[normalized_work_id] = uri
                result[result_key] = uri
                continue
            result[result_key] = self._publication_url(work)

        if unresolved_by_work_id:
            session = SessionLocal()
            try:
                records = session.execute(
                    select(
                        SourceRecord.work_id,
                        SourceRecord.source_name,
                        SourceRecord.source_record_id,
                        SourceRecord.source_url,
                        SourceRecord.raw_payload,
                        SourceRecord.doi,
                    )
                    .where(SourceRecord.work_id.in_(list(unresolved_by_work_id.keys())))
                ).all()
            finally:
                session.close()

            records_by_work_id: dict[int, list[dict]] = {}
            for work_id, source_name, source_record_id, source_url, raw_payload, doi in records:
                records_by_work_id.setdefault(work_id, []).append(
                    {
                        "source_name": source_name,
                        "source_record_id": source_record_id,
                        "source_url": source_url,
                        "raw_payload": raw_payload,
                        "doi": doi,
                    }
                )

            for work_id, grouped_works in unresolved_by_work_id.items():
                uri = self._profile_publication_uri_from_grouped_source_records(
                    grouped_works[0],
                    records_by_work_id.get(work_id, []),
                )
                if not uri:
                    uri = self._publication_url(grouped_works[0])
                self._profile_publication_uri_cache[work_id] = uri
                for work in grouped_works:
                    result[id(work)] = uri
        return result

    def _profile_publication_uri_from_grouped_source_records(
        self,
        work: Work | dict,
        records: list[SourceRecord | dict],
    ) -> str | None:
        for record in records:
            source_name = record.get("source_name") if isinstance(record, dict) else record.source_name
            raw_payload = record.get("raw_payload") if isinstance(record, dict) else record.raw_payload
            payload = raw_payload if isinstance(raw_payload, dict) else {}
            if source_name == "dspace":
                uri = self._dspace_record_url(payload, work, record)
                if uri:
                    return uri
        for record in records:
            source_name = record.get("source_name") if isinstance(record, dict) else record.source_name
            raw_payload = record.get("raw_payload") if isinstance(record, dict) else record.raw_payload
            source_url = record.get("source_url") if isinstance(record, dict) else record.source_url
            payload = raw_payload if isinstance(raw_payload, dict) else {}
            if source_name == "openalex":
                uri = self._first_url([payload.get("id"), source_url])
                if uri:
                    return uri
        return None

    def _profile_publication_uri_from_source_records(self, work_id: int, work: Work | dict) -> str | None:
        session = SessionLocal()
        try:
            records = session.execute(
                select(SourceRecord)
                .where(SourceRecord.work_id == work_id)
                .order_by(
                    case(
                        (SourceRecord.source_name == "dspace", 0),
                        (SourceRecord.source_name == "openalex", 1),
                        else_=2,
                    ),
                    SourceRecord.id.asc(),
                )
            ).scalars().all()
        finally:
            session.close()

        for record in records:
            payload = record.raw_payload if isinstance(record.raw_payload, dict) else {}
            if record.source_name == "dspace":
                uri = self._dspace_record_url(payload, work, record)
                if uri:
                    return uri
        for record in records:
            payload = record.raw_payload if isinstance(record.raw_payload, dict) else {}
            if record.source_name == "openalex":
                uri = self._first_url([payload.get("id"), record.source_url])
                if uri:
                    return uri
        return None

    def _dspace_record_url(self, payload: dict, work: Work | dict, source_record: SourceRecord) -> str | None:
        identifiers = self._dspace_values(payload, "identifier")
        dspace_url = self._first_dspace_platform_url(identifiers)
        if dspace_url:
            return dspace_url
        source_record_id = (
            source_record.get("source_record_id")
            if isinstance(source_record, dict)
            else source_record.source_record_id
        )
        source_record_url = self._dspace_uri_from_source_record_id(source_record_id)
        if source_record_url:
            return source_record_url
        non_doi_url = self._first_non_doi_url(identifiers)
        if non_doi_url:
            return non_doi_url
        source_url = source_record.get("source_url") if isinstance(source_record, dict) else source_record.source_url
        source_doi = source_record.get("doi") if isinstance(source_record, dict) else source_record.doi
        return self._publication_url(
            {
                "primary_source_url": self._work_value(work, "primary_source_url") or source_url,
                "doi": self._work_value(work, "doi") or source_doi,
            }
        )

    def _dspace_uri_from_work(self, work: Work | dict) -> str | None:
        source_record_id = self._work_value(work, "source_record_id")
        if source_record_id:
            return self._dspace_uri_from_source_record_id(str(source_record_id))
        return None

    def _dspace_uri_from_source_record_id(self, source_record_id: str | None) -> str | None:
        if not source_record_id:
            return None
        match = re.search(r"2263/(\d+)", str(source_record_id))
        if not match:
            return None
        return f"http://hdl.handle.net/2263/{match.group(1)}"

    def _first_dspace_platform_url(self, values: list | tuple) -> str | None:
        for value in values or []:
            url = self._first_url([value])
            if url and (
                "hdl.handle.net/" in url.lower()
                or "repository.up.ac.za/" in url.lower()
            ):
                return url
        return None

    def _first_non_doi_url(self, values: list | tuple) -> str | None:
        for value in values or []:
            url = self._first_url([value])
            if url and "doi.org/" not in url.lower():
                return url
        return None

    def _first_url(self, values: list | tuple) -> str | None:
        for value in values or []:
            if not value:
                continue
            text = " ".join(str(value).split()).strip()
            match = re.search(r"https?://[^\s<>\]\)'\"]+", text)
            if match:
                return match.group(0).rstrip(".,;")
        return None

    def _visible_mapping_label(self, value: str | None) -> str:
        cleaned = " ".join(str(value or "").split()).strip()
        if not cleaned or cleaned.lower() == "unmapped":
            return ""
        return cleaned

    def _is_article_work(self, work: Work | dict) -> bool:
        # Kept in sync with harmonizer.is_article_document_type â€” see that function's
        # comment for why "review" is listed explicitly (scope is "articles and reviews").
        return self._is_article_document_type(self._work_value(work, "document_type"))

    def _is_article_document_type(self, document_type: str | None) -> bool:
        if not document_type:
            return False
        normalized = str(document_type).strip().lower()
        if normalized in {"article", "journal-article", "review", "review-article"}:
            return True
        return "article" in normalized
    _title_stopwords = {
        "a", "an", "and", "the", "of", "in", "on", "for", "to", "with", "from", "by",
        "at", "as", "is", "are", "be", "this", "that", "these", "those",
    }
