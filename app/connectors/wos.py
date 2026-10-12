from __future__ import annotations

from datetime import date
from typing import Iterable

import requests

from app.config import settings
from app.connectors.base import BaseConnector
from app.schemas import HarmonizedPublication


class WebOfScienceConnector(BaseConnector):
    source_name = "web_of_science"
    base_url = settings.wos_base_url

    def fetch(
        self,
        limit: int = 100,
        state: dict | None = None,
    ) -> Iterable[HarmonizedPublication]:
        if not settings.wos_api_key:
            return []

        headers = {"X-ApiKey": settings.wos_api_key}
        publications: list[HarmonizedPublication] = []
        page = 1

        while len(publications) < limit:
            params = {
                "q": f"OG={settings.wos_organization}",
                "limit": min(limit - len(publications), 50),
                "page": page,
                "db": "WOS",
            }
            response = requests.get(self.base_url, headers=headers, params=params, timeout=60)
            response.raise_for_status()
            hits = response.json().get("hits", [])
            if not hits:
                break

            for hit in hits:
                names = [name.get("displayName") for name in hit.get("names", []) if name.get("displayName")]
                affiliations = [
                    affiliation.get("name")
                    for affiliation in hit.get("institutions", [])
                    if affiliation.get("name")
                ]
                identifiers = hit.get("identifiers", {})
                source = hit.get("source", {})
                publications.append(
                    HarmonizedPublication(
                        source_name=self.source_name,
                        source_record_id=hit.get("uid", ""),
                        title=hit.get("title"),
                        abstract=hit.get("abstract"),
                        doi=identifiers.get("doi"),
                        publication_year=self._parse_year(source.get("publishedBiblioDate")),
                        publication_date=self._parse_date(source.get("publishedBiblioDate")),
                        journal=source.get("sourceTitle"),
                        document_type=hit.get("documentType"),
                        language=hit.get("language"),
                        publisher=source.get("publisher"),
                        open_access_status=hit.get("openAccessType"),
                        citation_count=hit.get("citations", {}).get("count"),
                        authors=names,
                        affiliations=affiliations,
                        keywords=hit.get("keywords", []) or [],
                        source_url=hit.get("links", {}).get("record"),
                        raw_payload=hit,
                    )
                )

            page += 1
            if len(hits) < params["limit"]:
                break

        return publications

    def _parse_date(self, value: str | None) -> date | None:
        if not value:
            return None
        return date.fromisoformat(value[:10])

    def _parse_year(self, value: str | None) -> int | None:
        if not value:
            return None
        return int(value[:4])
