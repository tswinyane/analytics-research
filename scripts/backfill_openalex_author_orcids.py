from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import AuthorAlias, SourceRecord
from app.services.author_names import _clean_author_name, _name_fingerprint, _normalize_orcid


def main() -> None:
    init_db()
    inserted = 0
    updated = 0
    seen: set[tuple[str, str]] = set()
    session = SessionLocal()
    try:
        records = session.execute(
            select(SourceRecord.raw_payload).where(SourceRecord.source_name == "openalex")
        ).scalars()
        for payload in records:
            if not isinstance(payload, dict):
                continue
            for authorship in payload.get("authorships") or []:
                author = authorship.get("author") or {}
                canonical_name = _clean_author_name(author.get("display_name"))
                orcid = _normalize_orcid(author.get("orcid"))
                if not canonical_name or not orcid:
                    continue
                identity_keys = [f"orcid:{orcid}"]
                author_id = author.get("id")
                if author_id:
                    identity_keys.append(f"id:{author_id}")
                fingerprint = _name_fingerprint(canonical_name)
                if fingerprint:
                    identity_keys.append(f"fingerprint:{fingerprint}")

                for identity_key in identity_keys:
                    key = ("openalex", identity_key)
                    if key in seen:
                        continue
                    seen.add(key)
                    alias = session.scalar(
                        select(AuthorAlias).where(
                            AuthorAlias.source_name == "openalex",
                            AuthorAlias.identity_key == identity_key,
                        )
                    )
                    if alias is None:
                        session.add(
                            AuthorAlias(
                                source_name="openalex",
                                identity_key=identity_key,
                                canonical_name=canonical_name,
                            )
                        )
                        inserted += 1
                    elif alias.canonical_name != canonical_name:
                        alias.canonical_name = canonical_name
                        alias.updated_at = datetime.utcnow()
                        updated += 1
            if (inserted + updated) % 500 == 0:
                session.commit()
        session.commit()
    finally:
        session.close()

    print(f"Backfilled OpenAlex author ORCID aliases: inserted={inserted}, updated={updated}")


if __name__ == "__main__":
    main()
