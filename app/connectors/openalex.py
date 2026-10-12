from __future__ import annotations

import logging
import re
import time
from datetime import date
from email.utils import parsedate_to_datetime
from typing import Iterable

import requests

from app.config import settings
from app.connectors.base import BaseConnector
from app.schemas import HarmonizedPublication

logger = logging.getLogger(__name__)

class OpenAlexConnector(BaseConnector):
    source_name = "openalex"
    base_url = settings.openalex_base_url

    def __init__(self) -> None:
        self._last_request_at = 0.0

    def fetch(
        self,
        limit: int = 100,
        state: dict | None = None,
    ) -> Iterable[HarmonizedPublication]:
        cursor = "*"
        remaining = None if limit <= 0 else max(limit, 0)
        filters = [f"institutions.id:{settings.openalex_institution_id}"]

        from_date = (state or {}).get("last_publication_date")
        if from_date:
            filters.append(f"from_publication_date:{from_date}")

        fetched = 0
        page_number = 0
        while remaining is None or remaining > 0:
            per_page = settings.openalex_per_page if remaining is None else min(
                remaining,
                settings.openalex_per_page,
            )

            params = {
                "filter": ",".join(filters),
                "per-page": per_page,
                "cursor": cursor,
                "sort": "publication_date:asc",
            }

            response = self._get_with_retries(params)
            payload = response.json()
            results = payload.get("results", [])
            if not results:
                break
            page_number += 1
            fetched += len(results)
            logger.info(
                "OpenAlex institution progress: page %s, %s records received%s",
                page_number,
                fetched,
                f"/{limit}" if limit > 0 else "",
            )

            for item in results:
                yield self._publication_from_item(item)

            if remaining is not None:
                remaining -= len(results)

            cursor = payload.get("meta", {}).get("next_cursor")
            if not cursor:
                break

    def fetch_for_orcid(self, orcid: str) -> Iterable[HarmonizedPublication]:
        """All OpenAlex works with an authorship carrying this ORCID iD."""
        match = re.search(r"\d{4}-\d{4}-\d{4}-[\dXx]{4}", str(orcid or ""))
        if not match:
            return
        bare_orcid = match.group(0).upper()
        cursor = "*"
        fetched = 0
        page_number = 0
        while True:
            params = {
                "filter": f"authorships.author.orcid:https://orcid.org/{bare_orcid}",
                "per-page": settings.openalex_per_page,
                "cursor": cursor,
                "sort": "publication_date:asc",
            }
            payload = self._get_with_retries(params).json()
            results = payload.get("results", [])
            if not results:
                break
            page_number += 1
            fetched += len(results)
            logger.info("OpenAlex ORCID %s progress: page %s, %s records received", bare_orcid, page_number, fetched)
            for item in results:
                yield self._publication_from_item(item)
            cursor = payload.get("meta", {}).get("next_cursor")
            if not cursor:
                break

    def fetch_by_dois(self, dois: list[str]) -> list[HarmonizedPublication]:
        """Look up many DOIs at once (OpenAlex allows OR-ing up to 50 values per filter)."""
        cleaned: list[str] = []
        for doi in dois:
            value = self._clean_doi(doi)
            if value and "|" not in value and "," not in value and value not in cleaned:
                cleaned.append(value)
        publications: list[HarmonizedPublication] = []
        for start in range(0, len(cleaned), 50):
            batch = cleaned[start:start + 50]
            params = {"filter": "doi:" + "|".join(batch), "per-page": 100}
            try:
                payload = self._get_with_retries(params).json()
            except requests.exceptions.RequestException as exc:
                logger.warning("OpenAlex DOI batch lookup failed (%s DOIs): %s", len(batch), exc)
                continue
            publications.extend(self._publication_from_item(item) for item in payload.get("results", []))
        return publications

    def fetch_for_work(
        self,
        *,
        doi: str | None = None,
        title: str | None = None,
        year: int | None = None,
    ) -> HarmonizedPublication | None:
        return self.fetch_for_dspace_work(doi=doi, title=title, year=year)

    def fetch_for_dspace_work(
        self,
        *,
        doi: str | None = None,
        title: str | None = None,
        year: int | None = None,
    ) -> HarmonizedPublication | None:
        item = None
        clean_doi = self._clean_doi(doi)
        if clean_doi:
            item = self._fetch_one({"filter": f"doi:{clean_doi}"})
        if item is None and title:
            item = self._search_title(title, year)
        return self._publication_from_item(item) if item else None

    def fetch_for_author(
        self,
        author_id: str,
        *,
        limit: int = 0,
    ) -> Iterable[HarmonizedPublication]:
        normalized_author_id = self._openalex_author_id(author_id)
        if not normalized_author_id:
            return

        cursor = "*"
        remaining = None if limit <= 0 else max(limit, 0)
        fetched = 0
        page_number = 0
        while remaining is None or remaining > 0:
            per_page = settings.openalex_per_page if remaining is None else min(
                remaining,
                settings.openalex_per_page,
            )
            params = {
                "filter": f"authorships.author.id:{normalized_author_id}",
                "include_xpac": "true",
                "per-page": per_page,
                "cursor": cursor,
                "sort": "publication_date:asc",
            }
            response = self._get_with_retries(params)
            payload = response.json()
            results = payload.get("results", [])
            if not results:
                break
            page_number += 1
            fetched += len(results)
            logger.info(
                "OpenAlex author %s progress: page %s, %s records received",
                normalized_author_id,
                page_number,
                fetched,
            )

            for item in results:
                yield self._publication_from_item(item)

            if remaining is not None:
                remaining -= len(results)

            cursor = payload.get("meta", {}).get("next_cursor")
            if not cursor:
                break

    def build_next_state(
        self,
        publications: list[HarmonizedPublication],
        previous_state: dict | None = None,
    ) -> dict | None:
        previous_date = (previous_state or {}).get("last_publication_date")
        max_date = previous_date
        for publication in publications:
            if publication.publication_date is None:
                continue
            iso_date = publication.publication_date.isoformat()
            if max_date is None or iso_date > max_date:
                max_date = iso_date
        return {"last_publication_date": max_date} if max_date else previous_state

    def _get_with_retries(self, params: dict) -> requests.Response:
        timeout_seconds = 180
        max_attempts = max(settings.openalex_max_attempts, 1)
        last_error: Exception | None = None
        query_params = self._with_polite_pool_params(params)

        for attempt in range(max_attempts):
            try:
                self._throttle()
                logger.info("OpenAlex request: attempt %s/%s, timeout %ss", attempt + 1, max_attempts, timeout_seconds)
                response = requests.get(
                    self.base_url,
                    params=query_params,
                    headers=self._request_headers(),
                    timeout=timeout_seconds,
                )
                logger.info("OpenAlex response: HTTP %s", response.status_code)
                if response.status_code == 429:
                    remaining = response.headers.get("X-RateLimit-Remaining")
                    reset = response.headers.get("X-RateLimit-Reset")
                    if not settings.openalex_api_key:
                        raise RuntimeError(
                            "OpenAlex rejected the harvest because OPENALEX_API_KEY is not set. "
                            "Create a free key at https://openalex.org/settings/api, add it to "
                            ".env, and restart the harvest."
                        )
                    if remaining == "0":
                        raise RuntimeError(
                            "The OpenAlex API-key daily budget is exhausted. "
                            f"X-RateLimit-Reset={reset or 'unknown'} seconds. Resume the harvest "
                            "after the budget resets; do not use --reset-state."
                        )
                    last_error = requests.exceptions.HTTPError(
                        f"429 Too Many Requests for url: {response.url}",
                        response=response,
                    )
                    if attempt == max_attempts - 1:
                        response.raise_for_status()
                    delay = self._retry_delay_seconds(response, attempt)
                    logger.warning("OpenAlex rate limit: waiting %.1f seconds before retry", delay)
                    time.sleep(delay)
                    continue
                response.raise_for_status()
                return response
            except requests.exceptions.ReadTimeout as exc:
                logger.warning("OpenAlex read timeout on attempt %s/%s", attempt + 1, max_attempts)
                last_error = exc
                if attempt == max_attempts - 1:
                    raise
                time.sleep(2 * (attempt + 1))
            except requests.exceptions.ConnectTimeout as exc:
                logger.warning("OpenAlex connection timeout on attempt %s/%s", attempt + 1, max_attempts)
                last_error = exc
                if attempt == max_attempts - 1:
                    raise
                time.sleep(2 * (attempt + 1))
            except requests.exceptions.HTTPError as exc:
                last_error = exc
                response = exc.response
                if response is None or response.status_code not in {429, 500, 502, 503, 504}:
                    raise
                if attempt == max_attempts - 1:
                    raise
                delay = self._retry_delay_seconds(response, attempt)
                logger.warning("OpenAlex HTTP %s: waiting %.1f seconds before retry", response.status_code, delay)
                time.sleep(delay)
            except requests.exceptions.RequestException:
                raise

        if last_error is not None:
            raise last_error
        raise RuntimeError("OpenAlex request failed without a captured exception.")

    def _with_polite_pool_params(self, params: dict) -> dict:
        query_params = dict(params)
        if settings.openalex_mailto and "mailto" not in query_params:
            query_params["mailto"] = settings.openalex_mailto
        return query_params

    def _request_headers(self) -> dict[str, str]:
        if not settings.openalex_api_key:
            return {}
        return {"Authorization": f"Bearer {settings.openalex_api_key}"}

    def _throttle(self) -> None:
        delay = max(settings.openalex_request_delay_seconds, 0.0)
        if delay <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_request_at = time.monotonic()

    def _retry_delay_seconds(self, response: requests.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            parsed_delay = self._parse_retry_after_seconds(retry_after)
            if parsed_delay is not None:
                return min(parsed_delay, settings.openalex_retry_after_max_seconds)
        return min(30 * (attempt + 1), settings.openalex_retry_after_max_seconds)

    def _parse_retry_after_seconds(self, value: str) -> float | None:
        text = str(value or "").strip()
        if not text:
            return None
        if text.isdigit():
            return max(float(text), 0.0)
        try:
            retry_at = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            return None
        return max(retry_at.timestamp() - time.time(), 0.0)

    def _fetch_one(self, params: dict) -> dict | None:
        query_params = {"per-page": 1, **params}
        try:
            response = self._get_with_retries(query_params)
        except requests.exceptions.RequestException:
            return None
        results = response.json().get("results") or []
        return results[0] if results else None

    def _search_title(self, title: str, year: int | None = None) -> dict | None:
        filters: list[str] = []
        if year:
            filters.append(f"publication_year:{year}")
        params = {
            "search": title,
        }
        if filters:
            params["filter"] = ",".join(filters)
        return self._fetch_one(params)

    def _publication_from_item(self, item: dict) -> HarmonizedPublication:
        primary_location = item.get("primary_location") or {}
        source = primary_location.get("source") or {}
        return HarmonizedPublication(
            source_name=self.source_name,
            source_record_id=item.get("id", ""),
            title=item.get("display_name"),
            abstract=self._extract_abstract(item),
            doi=self._clean_doi(item.get("doi")),
            publication_year=item.get("publication_year"),
            publication_date=self._parse_date(item.get("publication_date")),
            journal=source.get("display_name"),
            document_type=item.get("type"),
            language=item.get("language"),
            publisher=source.get("host_organization_name"),
            open_access_status=(item.get("open_access") or {}).get("oa_status"),
            citation_count=item.get("cited_by_count"),
            authors=[
                author.get("author", {}).get("display_name")
                for author in item.get("authorships", [])
                if author.get("author", {}).get("display_name")
            ],
            affiliations=self._extract_affiliations(item),
            keywords=[
                concept.get("display_name")
                for concept in item.get("concepts", [])
                if concept.get("display_name")
            ],
            source_url=item.get("id"),
            raw_payload=self._compact_raw_payload(item),
        )

    def _extract_abstract(self, item: dict) -> str | None:
        inverted_index = item.get("abstract_inverted_index")
        if not inverted_index:
            return None

        words: list[tuple[int, str]] = []
        for word, positions in inverted_index.items():
            for position in positions:
                words.append((position, word))

        return " ".join(word for _, word in sorted(words))

    def _extract_affiliations(self, item: dict) -> list[str]:
        affiliations: list[str] = []
        for authorship in item.get("authorships", []):
            for institution in authorship.get("institutions", []):
                name = institution.get("display_name")
                if name and name not in affiliations:
                    affiliations.append(name)
        return affiliations

    def _compact_raw_payload(self, item: dict) -> dict:
        primary_location = item.get("primary_location") or {}
        source = primary_location.get("source") or {}
        return {
            "id": item.get("id"),
            "doi": item.get("doi"),
            "display_name": item.get("display_name"),
            "publication_year": item.get("publication_year"),
            "publication_date": item.get("publication_date"),
            "type": item.get("type"),
            "language": item.get("language"),
            "cited_by_count": item.get("cited_by_count"),
            "open_access": {
                "oa_status": (item.get("open_access") or {}).get("oa_status"),
            },
            "primary_location": {
                "source": {
                    "display_name": source.get("display_name"),
                    "host_organization_name": source.get("host_organization_name"),
                },
            },
            "authorships": [
                {
                    "author": {
                        "id": (authorship.get("author") or {}).get("id"),
                        "display_name": (authorship.get("author") or {}).get("display_name"),
                        "orcid": (authorship.get("author") or {}).get("orcid"),
                    },
                    "institutions": [
                        {
                            "id": institution.get("id"),
                            "display_name": institution.get("display_name"),
                            "ror": institution.get("ror"),
                            "country_code": institution.get("country_code"),
                            "type": institution.get("type"),
                        }
                        for institution in authorship.get("institutions", [])
                    ],
                }
                for authorship in item.get("authorships", [])
            ],
            "concepts": [
                {"display_name": concept.get("display_name")}
                for concept in item.get("concepts", [])
                if concept.get("display_name")
            ],
            "updated_date": item.get("updated_date"),
            "created_date": item.get("created_date"),
        }

    def _clean_doi(self, doi: str | None) -> str | None:
        if not doi:
            return None
        text = str(doi).strip()
        text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.IGNORECASE)
        text = re.sub(r"^doi:\s*", "", text, flags=re.IGNORECASE)
        return text.strip() or None

    def _openalex_author_id(self, value: str | None) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        match = re.search(r"A\d+", text, re.IGNORECASE)
        return match.group(0).upper() if match else text

    def _parse_date(self, value: str | None) -> date | None:
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
