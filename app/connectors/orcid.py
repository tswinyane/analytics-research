from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable

import requests

from app.config import settings
from app.connectors.base import BaseConnector
from app.schemas import HarmonizedPublication

logger = logging.getLogger(__name__)


class ORCIDConnector(BaseConnector):
    source_name = "orcid"

    def __init__(self) -> None:
        self.base_url = settings.orcid_base_url.rstrip("/")
        self._last_request_at = 0.0
        self._access_token = settings.orcid_access_token.strip() or None

    def fetch(
        self,
        limit: int = 100,
        state: dict | None = None,
    ) -> Iterable[HarmonizedPublication]:
        remaining = None if limit <= 0 else max(limit, 0)
        completed_orcids = {
            self._normalize_orcid(orcid)
            for orcid in (state or {}).get("completed_orcids", [])
            if self._normalize_orcid(orcid)
        }
        fetched_records = 0

        for person in self._search_institution_people():
            orcid = self._normalize_orcid(person.get("orcid"))
            if not orcid or orcid in completed_orcids:
                continue
            if remaining is not None and fetched_records >= remaining:
                break

            display_name = self._display_name(person)
            logger.info("Harvesting ORCID works for %s (%s)", display_name or orcid, orcid)
            for publication in self.fetch_for_orcid(orcid, display_name=display_name):
                if remaining is not None and fetched_records >= remaining:
                    break
                fetched_records += 1
                yield publication

            completed_orcids.add(orcid)

    def fetch_for_orcid(
        self,
        orcid: str,
        *,
        display_name: str | None = None,
    ) -> Iterable[HarmonizedPublication]:
        _, publications = self.fetch_researcher_works(orcid, display_name=display_name)
        yield from publications

    def search_researchers(self) -> Iterable[dict]:
        """Yield every researcher found by the ORCID institution search.

        Each item has a normalized bare ORCID iD under "orcid"; results without a valid
        iD are dropped so callers never see a researcher lacking one.
        """
        seen: set[str] = set()
        for person in self._search_institution_people():
            orcid = self._normalize_orcid(person.get("orcid"))
            if not orcid or orcid in seen:
                continue
            seen.add(orcid)
            affiliations = person.get("current_affiliations") or []
            if not isinstance(affiliations, list):
                affiliations = [affiliations]
            yield {
                "orcid": orcid,
                "given_names": self._clean(person.get("given_names")),
                "family_name": self._clean(person.get("family_name")),
                "credit_name": self._clean(person.get("credit_name")),
                "display_name": self._display_name(person),
                "affiliations": [str(value).strip() for value in affiliations if str(value or "").strip()],
            }

    def fetch_researcher_works(
        self,
        orcid: str,
        *,
        display_name: str | None = None,
    ) -> tuple[int | None, list[HarmonizedPublication]]:
        """Return (works last-modified epoch ms, publications) for one ORCID record."""
        normalized_orcid = self._normalize_orcid(orcid)
        if not normalized_orcid:
            return None, []

        response = self._get_with_retries(f"/{normalized_orcid}/works")
        payload = response.json()
        owner_name = display_name or self._display_name_from_record(payload) or normalized_orcid
        last_modified = self._last_modified(payload)

        publications: list[HarmonizedPublication] = []
        for group in payload.get("group") or []:
            summary = self._preferred_work_summary(group)
            if not summary:
                continue
            publication = self._publication_from_work_summary(
                summary,
                orcid=normalized_orcid,
                display_name=owner_name,
            )
            if publication is not None:
                publications.append(publication)
        return last_modified, publications

    def normalize_orcid(self, value: str | None) -> str:
        return self._normalize_orcid(value)

    def _last_modified(self, payload: dict) -> int | None:
        value = (payload.get("last-modified-date") or {}).get("value")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _clean(self, value) -> str | None:
        if isinstance(value, list):
            value = " ".join(str(item) for item in value if item)
        text = " ".join(str(value or "").split())
        return text or None

    def build_next_state(
        self,
        publications: list[HarmonizedPublication],
        previous_state: dict | None = None,
    ) -> dict | None:
        completed = {
            self._normalize_orcid(orcid)
            for orcid in (previous_state or {}).get("completed_orcids", [])
            if self._normalize_orcid(orcid)
        }
        for publication in publications:
            raw_payload = publication.raw_payload or {}
            orcid = self._normalize_orcid(raw_payload.get("orcid"))
            if orcid:
                completed.add(orcid)
        return {"completed_orcids": sorted(completed)} if completed else previous_state

    def authenticate_url(self, *, state: str | None = None) -> str:
        if not settings.orcid_client_id or not settings.orcid_redirect_uri:
            raise RuntimeError("ORCID_CLIENT_ID and ORCID_REDIRECT_URI must be set for ORCID sign-in.")
        params = {
            "client_id": settings.orcid_client_id,
            "response_type": "code",
            "scope": "/authenticate",
            "redirect_uri": settings.orcid_redirect_uri,
        }
        if state:
            params["state"] = state
        prepared = requests.Request("GET", settings.orcid_authorize_url, params=params).prepare()
        return prepared.url or settings.orcid_authorize_url

    def exchange_authorization_code(self, code: str) -> dict:
        if not settings.orcid_client_id or not settings.orcid_client_secret or not settings.orcid_redirect_uri:
            raise RuntimeError(
                "ORCID_CLIENT_ID, ORCID_CLIENT_SECRET, and ORCID_REDIRECT_URI must be set "
                "to exchange an ORCID authorization code."
            )
        response = requests.post(
            settings.orcid_oauth_token_url,
            data={
                "client_id": settings.orcid_client_id,
                "client_secret": settings.orcid_client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.orcid_redirect_uri,
            },
            headers={"Accept": "application/json"},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    # The ORCID search API cannot page past 10,000 results for one query (HTTP 400).
    SEARCH_RESULT_CAP = 10_000
    # ORCID went live in October 2012; no profile was submitted before this.
    SEARCH_EPOCH = datetime(2012, 1, 1, tzinfo=timezone.utc)

    def _search_institution_people(self) -> Iterable[dict]:
        """Yield every search hit, splitting large result sets by profile submission date
        so that each slice stays under the API's 10,000-result paging cap."""
        query = self._institution_query()
        if not query:
            return

        total = self._search_count(query)
        logger.info("ORCID search %s matched %s records", query, total)
        if total <= self.SEARCH_RESULT_CAP:
            yield from self._search_pages(query)
            return

        end = datetime.now(timezone.utc) + timedelta(days=1)
        pending: list[tuple[datetime, datetime]] = [(self.SEARCH_EPOCH, end)]
        while pending:
            lower, upper = pending.pop(0)
            sliced = self._submission_date_query(query, lower, upper)
            count = self._search_count(sliced)
            if count == 0:
                continue
            if count > self.SEARCH_RESULT_CAP and (upper - lower) > timedelta(days=1):
                middle = lower + (upper - lower) / 2
                pending[:0] = [(lower, middle), (middle, upper)]
                continue
            logger.info(
                "ORCID search slice %s to %s: %s records", lower.date(), upper.date(), count
            )
            yield from self._search_pages(sliced)

    def _submission_date_query(self, query: str, lower: datetime, upper: datetime) -> str:
        lower_text = "*" if lower <= self.SEARCH_EPOCH else lower.strftime("%Y-%m-%dT%H:%M:%SZ")
        upper_text = upper.strftime("%Y-%m-%dT%H:%M:%SZ")
        # Half-open range [lower, upper) so adjacent slices never overlap.
        return f"({query}) AND profile-submission-date:[{lower_text} TO {upper_text}}}"

    def _search_count(self, query: str) -> int:
        response = self._get_with_retries("/expanded-search/", params={"q": query, "start": 0, "rows": 1})
        try:
            return int(response.json().get("num-found") or 0)
        except (TypeError, ValueError):
            return 0

    def _search_pages(self, query: str) -> Iterable[dict]:
        start = 0
        # The expanded search allows up to 1,000 rows per request.
        page_size = min(max(settings.orcid_per_page, 1), 1000)
        while start < self.SEARCH_RESULT_CAP:
            rows = min(page_size, self.SEARCH_RESULT_CAP - start)
            try:
                response = self._get_with_retries(
                    "/expanded-search/",
                    params={"q": query, "start": start, "rows": rows},
                )
            except requests.exceptions.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 400 and start > 0:
                    logger.warning("ORCID search stopped at result %s for %s: %s", start, query, exc)
                    return
                raise
            payload = response.json()
            results = payload.get("expanded-result") or payload.get("result") or []
            if not results:
                return
            for result in results:
                yield self._person_from_search_result(result)
            if len(results) < rows:
                return
            start += len(results)

    def _institution_query(self) -> str:
        clauses: list[str] = []
        if settings.orcid_organization_ror:
            ror_id = settings.orcid_organization_ror.rstrip("/").rsplit("/", 1)[-1]
            clauses.append(f'ror-org-id:"{ror_id}"')
        if settings.orcid_organization_name:
            clauses.append(f'affiliation-org-name:"{settings.orcid_organization_name}"')
        return " OR ".join(clauses)

    def _person_from_search_result(self, result: dict) -> dict:
        return {
            "orcid": result.get("orcid-id") or result.get("orcid"),
            "given_names": result.get("given-names") or result.get("given-names-value"),
            "family_name": result.get("family-names") or result.get("family-name"),
            "credit_name": result.get("credit-name"),
            "current_affiliations": result.get("institution-name") or result.get("current-institution-affiliation-name"),
        }

    def _publication_from_work_summary(
        self,
        summary: dict,
        *,
        orcid: str,
        display_name: str,
    ) -> HarmonizedPublication | None:
        title = self._nested_value(summary, "title", "title", "value")
        if not title:
            return None

        put_code = str(summary.get("put-code") or "").strip()
        external_ids = self._external_ids(summary)
        doi = self._pick_external_id(external_ids, "doi")
        publication_date = self._publication_date(summary.get("publication-date"))
        source_record_id = f"{orcid}:{put_code}" if put_code else f"{orcid}:{self._title_key(title)}"
        source_url = f"https://orcid.org/{orcid}/work/{put_code}" if put_code else f"https://orcid.org/{orcid}"
        journal = self._nested_value(summary, "journal-title", "value")

        return HarmonizedPublication(
            source_name=self.source_name,
            source_record_id=source_record_id,
            title=title,
            doi=doi,
            publication_year=publication_date.year if publication_date else self._publication_year(summary.get("publication-date")),
            publication_date=publication_date,
            journal=journal,
            document_type=self._nested_value(summary, "type") or "work",
            authors=[display_name],
            source_url=source_url,
            raw_payload={
                "orcid": orcid,
                "display_name": display_name,
                "put_code": put_code,
                "external_ids": external_ids,
                "work_summary": summary,
                "authors": [
                    {
                        "orcid": f"https://orcid.org/{orcid}",
                        "display_name": display_name,
                    }
                ],
            },
        )

    def _get_with_retries(self, path: str, params: dict | None = None) -> requests.Response:
        max_attempts = max(settings.orcid_max_attempts, 1)
        last_error: Exception | None = None
        url = f"{self.base_url}{path if path.startswith('/') else '/' + path}"

        for attempt in range(max_attempts):
            try:
                self._throttle()
                response = requests.get(
                    url,
                    params=params,
                    headers=self._request_headers(),
                    timeout=60,
                )
                logger.info("ORCID response: HTTP %s for %s", response.status_code, path)
                if response.status_code in {429, 500, 502, 503, 504} and attempt < max_attempts - 1:
                    delay = self._retry_delay_seconds(response, attempt)
                    logger.warning("ORCID HTTP %s: waiting %.1f seconds before retry", response.status_code, delay)
                    time.sleep(delay)
                    continue
                response.raise_for_status()
                return response
            except requests.exceptions.RequestException as exc:
                last_error = exc
                if attempt == max_attempts - 1:
                    raise
                time.sleep(min(2 * (attempt + 1), settings.orcid_retry_after_max_seconds))

        if last_error is not None:
            raise last_error
        raise RuntimeError("ORCID request failed without a captured exception.")

    def _request_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.orcid+json"}
        token = self._read_public_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _read_public_token(self) -> str | None:
        if self._access_token:
            return self._access_token
        if not settings.orcid_client_id or not settings.orcid_client_secret:
            return None
        response = requests.post(
            settings.orcid_oauth_token_url,
            data={
                "client_id": settings.orcid_client_id,
                "client_secret": settings.orcid_client_secret,
                "grant_type": "client_credentials",
                "scope": "/read-public",
            },
            headers={"Accept": "application/json"},
            timeout=30,
        )
        response.raise_for_status()
        self._access_token = response.json().get("access_token")
        return self._access_token

    def _throttle(self) -> None:
        delay = max(settings.orcid_request_delay_seconds, 0.0)
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
                return min(parsed_delay, settings.orcid_retry_after_max_seconds)
        return min(10 * (attempt + 1), settings.orcid_retry_after_max_seconds)

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

    def _preferred_work_summary(self, group: dict) -> dict | None:
        summaries = group.get("work-summary") or []
        if not summaries:
            return None
        return sorted(
            summaries,
            key=lambda item: (
                item.get("display-index") is None,
                -int(item.get("display-index") or 0),
            ),
        )[0]

    def _display_name_from_record(self, payload: dict) -> str | None:
        person = payload.get("person") or {}
        name = person.get("name") or {}
        return self._display_name(
            {
                "credit_name": self._nested_value(name, "credit-name", "value"),
                "given_names": self._nested_value(name, "given-names", "value"),
                "family_name": self._nested_value(name, "family-name", "value"),
            }
        )

    def _display_name(self, person: dict) -> str | None:
        credit_name = str(person.get("credit_name") or "").strip()
        if credit_name:
            return credit_name
        parts = [
            str(person.get("given_names") or "").strip(),
            str(person.get("family_name") or "").strip(),
        ]
        display = " ".join(part for part in parts if part).strip()
        return display or None

    def _external_ids(self, summary: dict) -> list[dict]:
        container = summary.get("external-ids") or {}
        external_ids = container.get("external-id") or []
        return external_ids if isinstance(external_ids, list) else [external_ids]

    def _pick_external_id(self, external_ids: list[dict], external_id_type: str) -> str | None:
        for external_id in external_ids:
            if str(external_id.get("external-id-type") or "").lower() != external_id_type:
                continue
            value = self._nested_value(external_id, "external-id-value")
            if value:
                return value
        return None

    def _publication_date(self, value: dict | None) -> date | None:
        year = self._publication_year(value)
        if not year:
            return None
        month = self._date_part(value, "month") or 1
        day = self._date_part(value, "day") or 1
        try:
            return date(year, month, day)
        except ValueError:
            return date(year, 1, 1)

    def _publication_year(self, value: dict | None) -> int | None:
        return self._date_part(value, "year")

    def _date_part(self, value: dict | None, key: str) -> int | None:
        raw_value = self._nested_value(value or {}, key, "value")
        if raw_value is None:
            return None
        try:
            return int(raw_value)
        except (TypeError, ValueError):
            return None

    def _nested_value(self, value: dict, *keys: str) -> str | None:
        current = value
        for key in keys:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        if current is None:
            return None
        text = str(current).strip()
        return text or None

    def _normalize_orcid(self, value: str | None) -> str:
        match = re.search(r"\d{4}-\d{4}-\d{4}-[\dXx]{4}", str(value or ""))
        return match.group(0).upper() if match else ""

    def _title_key(self, value: str | None) -> str:
        text = str(value or "").lower()
        text = re.sub(r"[^a-z0-9\s]", " ", text)
        return "-".join(text.split()[:12])
