from __future__ import annotations

from datetime import date
from typing import Iterable

import requests

from app.config import settings
from app.connectors.base import BaseConnector
from app.schemas import HarmonizedPublication


class ScopusConnector(BaseConnector):
    source_name = "scopus"
    base_url = settings.scopus_base_url
    max_start = 5000

    def fetch(
        self,
        limit: int = 100,
        state: dict | None = None,
    ) -> Iterable[HarmonizedPublication]:
        if not settings.scopus_api_key:
            return []

        headers = {
            "X-ELS-APIKey": settings.scopus_api_key,
            "Accept": "application/json",
        }

        publications: list[HarmonizedPublication] = []
        start = 0
        query = self._build_query(state, limit)

        while limit <= 0 or len(publications) < limit:
            if start >= self.max_start:
                break

            remaining = limit - len(publications) if limit > 0 else 25
            batch_size = min(remaining, 25) if limit > 0 else 25
            if batch_size <= 0:
                break

            params = {
                "query": query,
                "count": batch_size,
                "start": start,
            }

            response = requests.get(self.base_url, headers=headers, params=params, timeout=60)
            if response.status_code == 400 and start >= self.max_start:
                break
            response.raise_for_status()

            entries = response.json().get("search-results", {}).get("entry", [])
            if not entries:
                break

            for entry in entries:
                publications.append(
                    HarmonizedPublication(
                        source_name=self.source_name,
                        source_record_id=entry.get("dc:identifier", ""),
                        title=entry.get("dc:title"),
                        abstract=entry.get("dc:description"),
                        doi=entry.get("prism:doi"),
                        publication_year=self._parse_year(entry.get("prism:coverDate")),
                        publication_date=self._parse_date(entry.get("prism:coverDate")),
                        journal=entry.get("prism:publicationName"),
                        document_type=entry.get("subtypeDescription"),
                        language=entry.get("language"),
                        publisher=entry.get("dc:publisher"),
                        open_access_status=str(entry.get("openaccessFlag")),
                        citation_count=self._parse_int(entry.get("citedby-count")),
                        authors=self._extract_authors(entry),
                        affiliations=self._extract_affiliations(entry),
                        keywords=self._extract_keywords(entry),
                        source_url=entry.get("prism:url"),
                        raw_payload=entry,
                    )
                )

            start += len(entries)
            if len(entries) < batch_size:
                break

        return publications

    def _build_query(self, state: dict | None, limit: int) -> str:
        query = f"AF-ID({settings.scopus_affiliation_id})"

        # Full backfill mode: when limit <= 0, fetch without the saved incremental checkpoint.
        if limit <= 0:
            return query

        last_cover_date = (state or {}).get("last_cover_date")
        if last_cover_date:
            return f"{query} AND PUBDATETXT(AFT {last_cover_date})"

        return query

    def build_next_state(
        self,
        publications: list[HarmonizedPublication],
        previous_state: dict | None = None,
    ) -> dict | None:
        latest = (previous_state or {}).get("last_cover_date")
        for publication in publications:
            if publication.publication_date is None:
                continue
            iso_date = publication.publication_date.isoformat()
            if latest is None or iso_date > latest:
                latest = iso_date
        return {"last_cover_date": latest} if latest else previous_state

    def _extract_authors(self, entry: dict) -> list[str]:
        creator = entry.get("dc:creator")
        if isinstance(creator, str):
            return [author.strip() for author in creator.split(";") if author.strip()]

        if isinstance(creator, list):
            results: list[str] = []
            for item in creator:
                if isinstance(item, str) and item.strip():
                    results.append(item.strip())
                elif isinstance(item, dict):
                    name = item.get("authname") or item.get("preferred-name")
                    if isinstance(name, str) and name.strip():
                        results.append(name.strip())
            return results

        return []

    def _extract_affiliations(self, entry: dict) -> list[str]:
        affiliation_data = entry.get("affiliation")
        if not affiliation_data:
            return []

        if isinstance(affiliation_data, list):
            results: list[str] = []
            for item in affiliation_data:
                if isinstance(item, dict):
                    name = item.get("affilname") or item.get("affiliation-name")
                    if isinstance(name, str) and name.strip():
                        results.append(name.strip())
                elif isinstance(item, str) and item.strip():
                    results.append(item.strip())
            return results

        if isinstance(affiliation_data, dict):
            name = affiliation_data.get("affilname") or affiliation_data.get("affiliation-name")
            if isinstance(name, str) and name.strip():
                return [name.strip()]
            return []

        if isinstance(affiliation_data, str) and affiliation_data.strip():
            return [affiliation_data.strip()]

        return []

    def _extract_keywords(self, entry: dict) -> list[str]:
        keywords = entry.get("authkeywords") or entry.get("keywords")
        if not keywords:
            return []

        if isinstance(keywords, str):
            return [keyword.strip() for keyword in keywords.split("|") if keyword.strip()]

        if isinstance(keywords, list):
            results: list[str] = []
            for item in keywords:
                if isinstance(item, str) and item.strip():
                    results.append(item.strip())
                elif isinstance(item, dict):
                    value = item.get("$") or item.get("keyword")
                    if isinstance(value, str) and value.strip():
                        results.append(value.strip())
            return results

        return []

    def _parse_date(self, value: str | None) -> date | None:
        if not value:
            return None
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None

    def _parse_year(self, value: str | None) -> int | None:
        if not value:
            return None
        try:
            return int(value[:4])
        except ValueError:
            return None

    def _parse_int(self, value: str | int | None) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
