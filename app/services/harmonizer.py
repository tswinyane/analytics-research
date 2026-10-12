from __future__ import annotations

import hashlib
import re

from app.models import Work
from app.schemas import HarmonizedPublication


DOI_PATTERN = re.compile(r"10\.\d{4,9}/\S+", re.IGNORECASE)


def is_article_document_type(document_type: str | None) -> bool:
    # Mirrors AnalyticsService._is_article_work's definition so "article" means the same
    # thing at ingestion time as it does in the existing "Article only" filter used
    # elsewhere in the app (e.g. the Publications page toggle) — one definition, not two.
    # Scope is "articles and reviews" (per explicit decision when cleaning up the existing
    # dataset) — OpenAlex's "review" type doesn't contain the substring "article", so it's
    # listed explicitly rather than relying on the substring check below to catch it.
    # Note the substring check is still a plain "article" in normalized, so a DSpace dc:type
    # value like "Newspaper Article" would also match; this repo's own AuthorAlias/author-
    # clustering code already has to explicitly exclude "newspaper"-tagged entries elsewhere,
    # so if that turns out to be a real category in your DSpace collection, tighten this to
    # an explicit allow-list of exact type strings instead of the substring check.
    if not document_type:
        return False
    normalized = str(document_type).strip().lower()
    if normalized in {"article", "journal-article", "review", "review-article"}:
        return True
    return "article" in normalized


def normalize_publication(publication: HarmonizedPublication) -> HarmonizedPublication:
    publication.title = _clean_text(publication.title)
    publication.abstract = _clean_text(publication.abstract)
    publication.doi = _normalize_doi(publication.doi)
    publication.authors = _dedupe(publication.authors)
    publication.affiliations = _dedupe(publication.affiliations)
    publication.keywords = _dedupe(publication.keywords)
    publication.journal = _clean_text(publication.journal)
    publication.publisher = _clean_text(publication.publisher)
    publication.document_type = _clean_text(publication.document_type)
    publication.language = _clean_text(publication.language)
    publication.open_access_status = _clean_text(
        str(publication.open_access_status) if publication.open_access_status is not None else None
    )
    return publication


def build_dedupe_key(publication: HarmonizedPublication) -> str:
    if publication.doi:
        digest = hashlib.sha256(publication.doi.encode("utf-8")).hexdigest()[:32]
        return f"doi:{digest}"

    title = _title_fingerprint(publication.title)
    signature = "|".join([title, str(publication.publication_year or "")])
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:24]
    return f"fallback:{digest}"


def merge_publication_into_work(work: Work, publication: HarmonizedPublication) -> None:
    work.title = _prefer_longer(work.title, publication.title)
    work.abstract = _prefer_longer(work.abstract, publication.abstract)
    work.doi = work.doi or publication.doi
    work.publication_year = work.publication_year or publication.publication_year
    work.publication_date = work.publication_date or publication.publication_date
    work.journal = work.journal or publication.journal
    work.document_type = work.document_type or publication.document_type
    work.language = work.language or publication.language
    work.publisher = work.publisher or publication.publisher
    work.open_access_status = work.open_access_status or publication.open_access_status
    work.citation_count = max(work.citation_count or 0, publication.citation_count or 0) or None
    work.authors = _merge_lists(work.authors, publication.authors)
    work.affiliations = _merge_lists(work.affiliations, publication.affiliations)
    work.keywords = _merge_lists(work.keywords, publication.keywords)
    work.primary_source_url = work.primary_source_url or publication.source_url
    work.source_names = _merge_lists(work.source_names, [publication.source_name])
    work.source_count = len(work.source_names or [])


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _title_fingerprint(value: str | None) -> str:
    if not value:
        return ""
    normalized = value.lower()
    normalized = re.sub(r"[^a-z0-9\s]", " ", normalized)
    stopwords = {
        "a", "an", "and", "the", "of", "in", "on", "for", "to", "with", "from", "by",
        "at", "as", "is", "are", "be", "this", "that", "these", "those",
    }
    tokens = [token for token in normalized.split() if token and token not in stopwords and not token.isdigit()]
    return " ".join(tokens[:24])


def _normalize_doi(value: str | None) -> str | None:
    if not value:
        return None

    cleaned = value.strip().lower()
    cleaned = cleaned.replace("https://doi.org/", "")
    cleaned = cleaned.replace("http://doi.org/", "")
    cleaned = cleaned.replace("doi:", "")
    cleaned = cleaned.strip()

    match = DOI_PATTERN.search(cleaned)
    if not match:
        return None

    doi = match.group(0).rstrip(".,;)")
    return doi


def _dedupe(values: list[str] | None) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()

    def visit(item):
        if item is None:
            return
        if isinstance(item, list):
            for nested in item:
                visit(nested)
            return
        if not isinstance(item, str):
            item = str(item)

        normalized = _clean_text(item)
        if normalized and normalized.lower() not in seen:
            cleaned.append(normalized)
            seen.add(normalized.lower())

    for value in values or []:
        visit(value)

    return cleaned


def _merge_lists(existing: list[str] | None, incoming: list[str] | None) -> list[str]:
    return _dedupe([*(existing or []), *(incoming or [])])


def _prefer_longer(existing: str | None, incoming: str | None) -> str | None:
    if not incoming:
        return existing
    if not existing:
        return incoming
    return incoming if len(incoming) > len(existing) else existing
