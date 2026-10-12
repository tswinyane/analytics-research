from __future__ import annotations

import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuthorAlias
from app.schemas import HarmonizedPublication


ALIAS_QUERY_BATCH_SIZE = 500


def standardize_openalex_authors(
    session: Session,
    publications: list[HarmonizedPublication],
) -> list[HarmonizedPublication]:
    return _standardize_authors(
        session,
        publications,
        source_name="openalex",
        extractor=_extract_openalex_author_identities,
    )


def standardize_orcid_authors(
    session: Session,
    publications: list[HarmonizedPublication],
) -> list[HarmonizedPublication]:
    return _standardize_authors(
        session,
        publications,
        source_name="orcid",
        extractor=_extract_orcid_author_identities,
    )


def _standardize_authors(
    session: Session,
    publications: list[HarmonizedPublication],
    *,
    source_name: str,
    extractor,
) -> list[HarmonizedPublication]:
    candidate_names: dict[str, str] = {}
    publication_identities: list[list[dict[str, str]]] = []

    for publication in publications:
        identities = extractor(publication)
        publication_identities.append(identities)
        for identity in identities:
            cleaned_name = identity["clean_name"]
            for key in identity["alias_keys"]:
                current = candidate_names.get(key)
                candidate_names[key] = _choose_better_name(current, cleaned_name)

    if not candidate_names:
        return publications

    existing_map = _load_aliases(
        session,
        source_name=source_name,
        identity_keys=list(candidate_names.keys()),
    )

    canonical_map: dict[str, str] = {}
    for identity_key, candidate_name in candidate_names.items():
        existing_alias = existing_map.get(identity_key)
        canonical_name = _choose_better_name(
            existing_alias.canonical_name if existing_alias else None,
            candidate_name,
        )
        canonical_map[identity_key] = canonical_name

        if existing_alias is None:
            session.add(
                AuthorAlias(
                    source_name=source_name,
                    identity_key=identity_key,
                    canonical_name=canonical_name,
                )
            )
        else:
            existing_alias.canonical_name = canonical_name

    session.flush()

    for publication, identities in zip(publications, publication_identities, strict=False):
        standardized_authors: list[str] = []
        for identity in identities:
            canonical_name = None
            for key in identity["alias_keys"]:
                canonical_name = canonical_map.get(key)
                if canonical_name:
                    break
            canonical_name = canonical_name or identity["clean_name"]
            if canonical_name and canonical_name not in standardized_authors:
                standardized_authors.append(canonical_name)

        if standardized_authors:
            publication.authors = standardized_authors

    return publications


def canonicalize_author_names(
    session: Session,
    source_name: str,
    authors: list[str] | None,
) -> list[str]:
    if not authors:
        return []

    alias_keys = [f"fingerprint:{_name_fingerprint(author)}" for author in authors if _name_fingerprint(author)]
    if not alias_keys:
        return [_clean_author_name(author) for author in authors if _clean_author_name(author)]

    alias_rows = _load_aliases(
        session,
        source_name=source_name,
        identity_keys=alias_keys,
    )
    alias_map = {identity_key: alias.canonical_name for identity_key, alias in alias_rows.items()}

    canonical_names: list[str] = []
    for author in authors:
        cleaned_name = _clean_author_name(author)
        if not cleaned_name:
            continue
        fingerprint = _name_fingerprint(cleaned_name)
        canonical_name = alias_map.get(f"fingerprint:{fingerprint}", cleaned_name)
        if canonical_name not in canonical_names:
            canonical_names.append(canonical_name)
    return canonical_names


def _extract_openalex_author_identities(publication: HarmonizedPublication) -> list[dict[str, str]]:
    raw_payload = publication.raw_payload or {}
    identities: list[dict[str, str]] = []
    for authorship in raw_payload.get("authorships", []):
        author = authorship.get("author") or {}
        display_name = author.get("display_name")
        clean_name = _clean_author_name(display_name)
        if not clean_name:
            continue

        alias_keys: list[str] = []
        author_id = author.get("id")
        if author_id:
            alias_keys.append(f"id:{author_id}")
        orcid = _normalize_orcid(author.get("orcid"))
        if orcid:
            alias_keys.append(f"orcid:{orcid}")

        fingerprint = _name_fingerprint(clean_name)
        if fingerprint:
            alias_keys.append(f"fingerprint:{fingerprint}")

        if alias_keys:
            identities.append(
                {
                    "clean_name": clean_name,
                    "alias_keys": alias_keys,
                }
            )
    return identities


def _extract_orcid_author_identities(publication: HarmonizedPublication) -> list[dict[str, str]]:
    raw_payload = publication.raw_payload or {}
    identities: list[dict[str, str]] = []
    authors = raw_payload.get("authors") or []
    if not authors and raw_payload.get("orcid"):
        authors = [
            {
                "orcid": raw_payload.get("orcid"),
                "display_name": raw_payload.get("display_name"),
            }
        ]

    for author in authors:
        display_name = author.get("display_name")
        clean_name = _clean_author_name(display_name)
        if not clean_name:
            continue

        alias_keys: list[str] = []
        orcid = _normalize_orcid(author.get("orcid"))
        if orcid:
            alias_keys.append(f"orcid:{orcid}")

        fingerprint = _name_fingerprint(clean_name)
        if fingerprint:
            alias_keys.append(f"fingerprint:{fingerprint}")

        if alias_keys:
            identities.append(
                {
                    "clean_name": clean_name,
                    "alias_keys": alias_keys,
                }
            )
    return identities


def _clean_author_name(value: str | None) -> str | None:
    if not value:
        return None

    cleaned = " ".join(value.replace(",", ", ").split())
    if "," in cleaned:
        last_name, given_names = [part.strip() for part in cleaned.split(",", 1)]
        cleaned = f"{given_names} {last_name}".strip()

    tokens = [token for token in cleaned.split(" ") if token]
    formatted_tokens: list[str] = []
    for token in tokens:
        stripped = token.strip(".")
        if not stripped:
            continue
        if len(stripped) == 1 and stripped.isalpha():
            formatted_tokens.append(f"{stripped.upper()}.")
            continue

        parts = [
            part[:1].upper() + part[1:].lower()
            for part in stripped.split("-")
            if part
        ]
        formatted_tokens.append("-".join(parts))

    result = " ".join(formatted_tokens).strip()
    return result or None


def _normalize_orcid(value: str | None) -> str:
    if not value:
        return ""
    cleaned = str(value).strip()
    if not cleaned:
        return ""
    orcid_match = re.search(r"\d{4}-\d{4}-\d{4}-[\dXx]{4}", cleaned)
    if not orcid_match:
        return cleaned
    return f"https://orcid.org/{orcid_match.group(0).upper()}"


def _name_fingerprint(value: str | None) -> str:
    if not value:
        return ""

    normalized = unicodedata.normalize("NFKD", value)
    normalized = normalized.encode("ascii", "ignore").decode("ascii")
    normalized = normalized.lower()
    normalized = re.sub(r"[^a-z,\s-]", " ", normalized)
    normalized = " ".join(normalized.split())
    if not normalized:
        return ""

    if "," in normalized:
        last_name, given_names = [part.strip() for part in normalized.split(",", 1)]
        parts = [part for part in given_names.split(" ") if part]
        surname = last_name
    else:
        tokens = [token for token in normalized.split(" ") if token]
        if not tokens:
            return ""
        surname = tokens[-1]
        parts = tokens[:-1]

    initials = "".join(part[0] for part in parts if part)
    return f"{surname}|{initials}"


def _choose_better_name(existing: str | None, candidate: str | None) -> str:
    if not candidate:
        return existing or ""
    if not existing:
        return candidate

    existing_score = _name_score(existing)
    candidate_score = _name_score(candidate)
    return candidate if candidate_score > existing_score else existing


def _name_score(value: str) -> tuple[int, int, int]:
    tokens = [token.strip(".") for token in value.split() if token]
    full_tokens = sum(1 for token in tokens if len(token) > 1)
    total_letters = sum(len(token) for token in tokens)
    initial_penalty = -sum(1 for token in tokens if len(token) == 1)
    return (full_tokens, total_letters, initial_penalty)


def _load_aliases(
    session: Session,
    *,
    source_name: str,
    identity_keys: list[str],
) -> dict[str, AuthorAlias]:
    aliases: dict[str, AuthorAlias] = {}
    for start in range(0, len(identity_keys), ALIAS_QUERY_BATCH_SIZE):
        batch = identity_keys[start:start + ALIAS_QUERY_BATCH_SIZE]
        if not batch:
            continue
        rows = session.execute(
            select(AuthorAlias).where(
                AuthorAlias.source_name == source_name,
                AuthorAlias.identity_key.in_(batch),
            )
        ).scalars()
        for alias in rows:
            aliases[alias.identity_key] = alias
    return aliases
