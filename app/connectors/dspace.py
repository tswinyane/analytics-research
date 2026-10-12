from __future__ import annotations

from datetime import date
from typing import Iterable
from xml.etree import ElementTree as ET

import requests

from app.config import settings
from app.connectors.base import BaseConnector
from app.schemas import HarmonizedPublication


class DSpaceConnector(BaseConnector):
    source_name = "dspace"
    namespaces = {
        "oai": "http://www.openarchives.org/OAI/2.0/",
        "dc": "http://purl.org/dc/elements/1.1/",
    }

    def fetch(
        self,
        limit: int = 100,
        state: dict | None = None,
    ) -> Iterable[HarmonizedPublication]:
        publications: list[HarmonizedPublication] = []
        resumption_token: str | None = None
        unlimited = limit <= 0

        while unlimited or len(publications) < limit:
            params = self._build_params(resumption_token, state)
            response = requests.get(settings.dspace_endpoint, params=params, timeout=120)
            response.raise_for_status()

            root = ET.fromstring(response.text)
            records = root.findall(".//oai:record", self.namespaces)
            if not records:
                break

            for record in records:
                publication = self._parse_record(record)
                if publication is None:
                    continue
                publications.append(publication)
                if not unlimited and len(publications) >= limit:
                    break

            token_node = root.find(".//oai:resumptionToken", self.namespaces)
            resumption_token = self._text(token_node)
            if not resumption_token or (not unlimited and len(publications) >= limit):
                break

        return publications

    def build_next_state(
        self,
        publications: list[HarmonizedPublication],
        previous_state: dict | None = None,
    ) -> dict | None:
        latest = (previous_state or {}).get("last_datestamp")
        for publication in publications:
            raw_payload = publication.raw_payload or {}
            datestamp = raw_payload.get("_oai_datestamp")
            if datestamp and (latest is None or datestamp > latest):
                latest = datestamp
        return {"last_datestamp": latest} if latest else previous_state

    def _build_params(
        self,
        resumption_token: str | None,
        state: dict | None = None,
    ) -> dict[str, str]:
        if resumption_token:
            return {
                "verb": "ListRecords",
                "resumptionToken": resumption_token,
            }

        params = {
            "verb": "ListRecords",
            "metadataPrefix": settings.dspace_metadata_prefix,
        }
        if settings.dspace_set_spec:
            params["set"] = settings.dspace_set_spec
        if state and state.get("last_datestamp"):
            params["from"] = state["last_datestamp"]
        return params

    def _parse_record(self, record: ET.Element) -> HarmonizedPublication | None:
        header = record.find("oai:header", self.namespaces)
        metadata = record.find("oai:metadata", self.namespaces)
        if header is None or metadata is None:
            return None

        dc = metadata.find(".//dc:title/..", self.namespaces)
        values = self._extract_dc_values(dc) if dc is not None else {}
        datestamp = self._text(header.find("oai:datestamp", self.namespaces))
        values["_oai_datestamp"] = datestamp

        return HarmonizedPublication(
            source_name=self.source_name,
            source_record_id=self._text(header.find("oai:identifier", self.namespaces)) or "",
            title=self._first(values.get("title")),
            abstract=self._first(values.get("description")),
            doi=self._extract_doi(values.get("identifier", [])),
            publication_year=self._parse_year(self._first(values.get("date"))),
            publication_date=self._parse_date(self._first(values.get("date"))),
            journal=self._first(values.get("source")),
            document_type=self._first(values.get("type")),
            language=self._first(values.get("language")),
            publisher=self._first(values.get("publisher")),
            open_access_status="repository",
            citation_count=None,
            authors=values.get("creator", []),
            affiliations=[],
            keywords=values.get("subject", []),
            source_url=self._first(values.get("identifier")),
            raw_payload=values,
        )

    def _extract_dc_values(self, node: ET.Element) -> dict[str, list[str]]:
        values: dict[str, list[str]] = {}
        for child in node:
            tag = child.tag.split("}", 1)[-1]
            text = self._text(child)
            if text:
                values.setdefault(tag, []).append(text)
        return values

    def _extract_doi(self, identifiers: list[str]) -> str | None:
        for identifier in identifiers:
            lowered = identifier.lower()
            if "doi.org/" in lowered:
                return identifier.rsplit("/", 1)[-1]
            if lowered.startswith("10."):
                return identifier
        return None

    def _first(self, values: list[str] | None) -> str | None:
        if not values:
            return None
        return values[0]

    def _parse_date(self, value: str | None) -> date | None:
        if not value:
            return None
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None

    def _parse_year(self, value: str | None) -> int | None:
        if not value or len(value) < 4 or not value[:4].isdigit():
            return None
        return int(value[:4])

    def _text(self, node: ET.Element | None) -> str | None:
        if node is None or node.text is None:
            return None
        return node.text.strip() or None
