from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Query
from fastapi import HTTPException
from fastapi import Response
from fastapi.responses import FileResponse

from app.api.schemas import HarvestRequest
from app.config import settings
from app.services.analytics import AnalyticsService
from app.services.excel_export import build_article_metadata_workbook
from app.services.pipeline import IngestionPipeline


router = APIRouter(prefix="/api")
analytics = AnalyticsService()
pipeline = IngestionPipeline()
ARTICLE_METADATA_WORKBOOK = Path("outputs/article_metadata_export/article_metadata_full.xlsx")


def _config_payload(faculty: str | None = None) -> dict:
    return {
        "sources": pipeline.available_sources(),
        "default_sources": pipeline.default_sources(),
        "faculties": analytics.faculties(),
        "departments": analytics.departments(faculty),
        "schedule": {
            "hour": settings.harvest_schedule_hour,
            "minute": settings.harvest_schedule_minute,
        },
        "institution_id": settings.openalex_institution_id,
        "orcid": {
            "organization_name": settings.orcid_organization_name,
            "organization_ror": settings.orcid_organization_ror,
            "sign_in_enabled": bool(settings.orcid_client_id and settings.orcid_redirect_uri),
        },
    }


@router.get("/dashboard")
def dashboard_bundle(
    faculty: str | None = None,
    department: str | None = None,
    publication_year: list[int] | None = Query(default=None),
    publication_years: str | None = None,
    article_only: bool = False,
) -> dict:
    payload = analytics.dashboard_bundle(
        faculty=faculty,
        department=department,
        publication_years=_parse_publication_years(publication_year, publication_years),
        article_only=article_only,
    )
    response_payload = dict(payload)
    response_payload["config"] = _config_payload(faculty)
    return response_payload


@router.get("/config")
def read_config(faculty: str | None = None) -> dict:
    return _config_payload(faculty)


@router.get("/auth/orcid/login")
def orcid_login(state: str | None = None) -> dict:
    connector = pipeline.connectors["orcid"]
    try:
        return {"authorization_url": connector.authenticate_url(state=state)}
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/auth/orcid/callback")
def orcid_callback(code: str, state: str | None = None) -> dict:
    connector = pipeline.connectors["orcid"]
    try:
        token_payload = connector.exchange_authorization_code(code)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"ORCID token exchange failed: {exc}") from exc

    return {
        "orcid": token_payload.get("orcid"),
        "name": token_payload.get("name"),
        "scope": token_payload.get("scope"),
        "state": state,
        "authenticated": bool(token_payload.get("orcid")),
    }


@router.get("/overview")
def read_overview(
    faculty: str | None = None,
    department: str | None = None,
    article_only: bool = False,
) -> dict:
    return analytics.overview(faculty=faculty, department=department, article_only=article_only)


@router.get("/analytics/sources")
def source_breakdown(
    faculty: str | None = None,
    department: str | None = None,
    article_only: bool = False,
) -> list[dict]:
    return analytics.by_source(faculty=faculty, department=department, article_only=article_only)


@router.get("/analytics/years")
def yearly_breakdown(
    faculty: str | None = None,
    department: str | None = None,
    article_only: bool = False,
) -> list[dict]:
    return analytics.by_year(faculty=faculty, department=department, article_only=article_only)


@router.get("/analytics/faculties")
def faculty_breakdown() -> list[dict]:
    return analytics.by_faculty()


@router.get("/analytics/departments")
def department_breakdown(faculty: str | None = None, article_only: bool = False) -> list[dict]:
    return analytics.by_department(faculty=faculty, article_only=article_only)


@router.get("/analytics/document-types")
def document_type_breakdown(
    faculty: str | None = None,
    department: str | None = None,
    article_only: bool = False,
) -> list[dict]:
    return analytics.by_document_type(faculty=faculty, department=department, article_only=article_only)


@router.get("/analytics/journals")
def top_journals(
    limit: int = 10,
    faculty: str | None = None,
    department: str | None = None,
    article_only: bool = False,
) -> list[dict]:
    return analytics.top_journals(limit=limit, faculty=faculty, department=department, article_only=article_only)


@router.get("/analytics/authors")
def top_authors(
    limit: int = 10,
    faculty: str | None = None,
    department: str | None = None,
    article_only: bool = False,
) -> list[dict]:
    return analytics.top_authors(limit=limit, faculty=faculty, department=department, article_only=article_only)


@router.get("/analytics/open-access")
def open_access_breakdown(
    faculty: str | None = None,
    department: str | None = None,
    article_only: bool = False,
) -> list[dict]:
    return analytics.open_access_breakdown(faculty=faculty, department=department, article_only=article_only)


@router.get("/works/recent")
def recent_works(
    limit: int = 25,
    faculty: str | None = None,
    department: str | None = None,
    publication_year: list[int] | None = Query(default=None),
    publication_years: str | None = None,
    article_only: bool = False,
) -> list[dict]:
    return analytics.recent_works(
        limit=limit,
        faculty=faculty,
        department=department,
        publication_years=_parse_publication_years(publication_year, publication_years),
        article_only=article_only,
    )


@router.get("/publications")
def publications(
    limit: int = 100,
    faculty: str | None = None,
    department: str | None = None,
    publication_year: list[int] | None = Query(default=None),
    publication_years: str | None = None,
    article_only: bool = True,
) -> list[dict]:
    rows = analytics.publications(
        limit=limit,
        faculty=faculty,
        department=department,
        publication_years=_parse_publication_years(publication_year, publication_years),
        article_only=article_only,
    )
    return [
        row
        for row in rows
        if _is_visible_mapping_label(row.get("faculty"))
    ]


@router.get("/publications/browse")
def publications_browse(
    page: int = 1,
    page_size: int = 20,
    faculty: str | None = None,
    department: str | None = None,
    publication_year: list[int] | None = Query(default=None),
    publication_years: str | None = None,
    alphabet: str | None = None,
    search: str | None = None,
    article_only: bool = True,
) -> dict:
    return analytics.publications_browse(
        page=page,
        page_size=page_size,
        faculty=faculty,
        department=department,
        publication_years=_parse_publication_years(publication_year, publication_years),
        alphabet=alphabet,
        search=search,
        article_only=article_only,
    )


@router.get("/articles/by-document-type")
def articles_by_document_type(
    faculty: str | None = None,
    department: str | None = None,
    publication_year: list[int] | None = Query(default=None),
    publication_years: str | None = None,
) -> list[dict]:
    return analytics.articles_by_document_type(
        faculty=faculty,
        department=department,
        publication_years=_parse_publication_years(publication_year, publication_years),
    )


@router.get("/articles/by-document-type/download")
def download_articles_by_document_type(
    faculty: str | None = None,
    department: str | None = None,
    publication_year: list[int] | None = Query(default=None),
    publication_years: str | None = None,
) -> Response:
    rows = analytics.article_metadata_rows(
        faculty=faculty,
        department=department,
        publication_years=_parse_publication_years(publication_year, publication_years),
    )
    workbook = build_article_metadata_workbook(rows)
    return Response(
        content=workbook,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="article_metadata_by_type.xlsx"',
        },
    )


@router.get("/publications/article-metadata/download")
def download_article_metadata_workbook() -> FileResponse:
    if not ARTICLE_METADATA_WORKBOOK.exists():
        raise HTTPException(status_code=404, detail="Article metadata workbook has not been generated yet.")
    return FileResponse(
        ARTICLE_METADATA_WORKBOOK,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="article_metadata_full.xlsx",
    )


@router.get("/researchers")
def researchers(
    response: Response,
    limit: int = 50,
    page: int | None = None,
    page_size: int | None = None,
    faculty: str | None = None,
    department: str | None = None,
    publication_year: list[int] | None = Query(default=None),
    publication_years: str | None = None,
    article_only: bool = False,
    researcher: str | None = None,
    alphabet: str | None = None,
) -> list[dict] | dict:
    parsed_publication_years = _parse_publication_years(publication_year, publication_years)
    if page is not None or page_size is not None or alphabet:
        data = analytics.researchers_browse(
            page=page or 1,
            page_size=page_size or limit,
            faculty=faculty,
            department=department,
            publication_years=parsed_publication_years,
            article_only=article_only,
            researcher=researcher,
            alphabet=alphabet,
        )
    else:
        data = analytics.researchers(
            limit=limit,
            faculty=faculty,
            department=department,
            publication_years=parsed_publication_years,
            article_only=article_only,
            researcher=researcher,
        )
    response.headers["X-Researchers-Cache"] = analytics.last_researchers_cache_status()
    return data


@router.get("/research-fields")
def research_fields(
    limit: int = 50,
    faculty: str | None = None,
    department: str | None = None,
    publication_year: list[int] | None = Query(default=None),
    publication_years: str | None = None,
    article_only: bool = False,
) -> list[dict]:
    return analytics.research_fields(
        limit=limit,
        faculty=faculty,
        department=department,
        publication_years=_parse_publication_years(publication_year, publication_years),
        article_only=article_only,
    )


@router.get("/researchers/profile")
def researcher_profile(
    researcher: str,
    faculty: str | None = None,
    department: str | None = None,
    publication_year: list[int] | None = Query(default=None),
    publication_years: str | None = None,
    article_only: bool = False,
    publication_title: str | None = None,
    orcid: str | None = None,
) -> dict:
    return analytics.researcher_profile(
        researcher=researcher,
        faculty=faculty,
        department=department,
        publication_years=_parse_publication_years(publication_year, publication_years),
        article_only=article_only,
        publication_title=publication_title,
        orcid=orcid,
    )


@router.get("/runs/recent")
def recent_runs(limit: int = 10) -> list[dict]:
    return analytics.recent_runs(limit=limit)


@router.post("/harvest/run")
def run_harvest(request: HarvestRequest) -> dict:
    result = pipeline.ingest_sources(
        source_names=request.sources,
        limit=request.limit,
        article_only=request.article_only,
        reset_state=request.reset_state,
    )
    analytics.clear_cache()
    return result


def _parse_publication_years(
    repeated_years: list[int] | None,
    comma_separated_years: str | None,
) -> list[int]:
    years = list(repeated_years or [])
    if comma_separated_years:
        for raw_year in comma_separated_years.split(","):
            cleaned = raw_year.strip()
            if cleaned:
                years.append(int(cleaned))
    return sorted(set(years))


def _is_visible_mapping_label(value: str | None) -> bool:
    cleaned = " ".join(str(value or "").split()).strip()
    return bool(cleaned) and cleaned.lower() != "unmapped"
