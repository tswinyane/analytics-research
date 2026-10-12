from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from urllib.parse import quote_plus

load_dotenv()


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "UP Research Analytics")
    api_port: int = int(os.getenv("API_PORT", "8000"))
    frontend_port: int = int(os.getenv("FRONTEND_PORT", "3000"))
    mysql_host: str = os.getenv("MYSQL_HOST", "localhost")
    mysql_port: int = int(os.getenv("MYSQL_PORT", "3306"))
    mysql_user: str = os.getenv("MYSQL_USER", "root")
    mysql_password: str = os.getenv("MYSQL_PASSWORD", "")
    mysql_database: str = os.getenv("MYSQL_DATABASE", "up_research_analytics")
    harvest_default_limit: int = int(os.getenv("HARVEST_DEFAULT_LIMIT", "100"))
    harvest_schedule_hour: int = int(os.getenv("HARVEST_SCHEDULE_HOUR", "2"))
    harvest_schedule_minute: int = int(os.getenv("HARVEST_SCHEDULE_MINUTE", "0"))
    run_harvest_on_startup: bool = os.getenv("RUN_HARVEST_ON_STARTUP", "false").lower() == "true"
    init_db_on_startup: bool = os.getenv("INIT_DB_ON_STARTUP", "false").lower() == "true"
    openalex_base_url: str = os.getenv("OPENALEX_BASE_URL", "https://api.openalex.org/works")
    openalex_institution_id: str = os.getenv("OPENALEX_INSTITUTION_ID", "I69552723")
    openalex_api_key: str = os.getenv("OPENALEX_API_KEY", "")
    openalex_per_page: int = int(os.getenv("OPENALEX_PER_PAGE", "200"))
    openalex_mailto: str = os.getenv("OPENALEX_MAILTO", os.getenv("OPENALEX_EMAIL", ""))
    openalex_request_delay_seconds: float = float(os.getenv("OPENALEX_REQUEST_DELAY_SECONDS", "0.05"))
    openalex_max_attempts: int = int(os.getenv("OPENALEX_MAX_ATTEMPTS", "8"))
    openalex_retry_after_max_seconds: int = int(os.getenv("OPENALEX_RETRY_AFTER_MAX_SECONDS", "60"))
    openalex_dspace_enrichment_limit: int = int(os.getenv("OPENALEX_DSPACE_ENRICHMENT_LIMIT", "0"))
    openalex_doi_enrichment_limit: int = int(os.getenv("OPENALEX_DOI_ENRICHMENT_LIMIT", "0"))
    # Max ORCID/DSpace works without a DOI match to look up individually in OpenAlex by
    # title per run (slow, one request each). 0 skips the title-search phase.
    openalex_title_enrichment_limit: int = int(
        os.getenv("OPENALEX_TITLE_ENRICHMENT_LIMIT", os.getenv("OPENALEX_DSPACE_ENRICHMENT_LIMIT", "0"))
    )
    # Keep the institution-wide OpenAlex harvest after the ORCID-driven enrichment.
    openalex_institution_harvest: bool = os.getenv("OPENALEX_INSTITUTION_HARVEST", "true").lower() == "true"
    # Pulling every mapped researcher's OpenAlex corpus is slow and duplicate-prone during
    # a clean rebuild. Keep it opt-in; the institution harvest is the fast bulk path.
    openalex_researcher_expansion: bool = os.getenv("OPENALEX_RESEARCHER_EXPANSION", "false").lower() == "true"
    openalex_researchers_per_run: int = int(os.getenv("OPENALEX_RESEARCHERS_PER_RUN", "0"))
    orcid_base_url: str = os.getenv("ORCID_BASE_URL", "https://pub.orcid.org/v3.0")
    orcid_oauth_token_url: str = os.getenv("ORCID_OAUTH_TOKEN_URL", "https://orcid.org/oauth/token")
    orcid_authorize_url: str = os.getenv("ORCID_AUTHORIZE_URL", "https://orcid.org/oauth/authorize")
    orcid_client_id: str = os.getenv("ORCID_CLIENT_ID", "")
    orcid_client_secret: str = os.getenv("ORCID_CLIENT_SECRET", "")
    orcid_redirect_uri: str = os.getenv("ORCID_REDIRECT_URI", "")
    orcid_access_token: str = os.getenv("ORCID_ACCESS_TOKEN", "")
    orcid_organization_name: str = os.getenv("ORCID_ORGANIZATION_NAME", "University of Pretoria")
    orcid_organization_ror: str = os.getenv("ORCID_ORGANIZATION_ROR", "")
    orcid_per_page: int = int(os.getenv("ORCID_PER_PAGE", "50"))
    orcid_request_delay_seconds: float = float(os.getenv("ORCID_REQUEST_DELAY_SECONDS", "0.5"))
    orcid_max_attempts: int = int(os.getenv("ORCID_MAX_ATTEMPTS", "5"))
    orcid_retry_after_max_seconds: int = int(os.getenv("ORCID_RETRY_AFTER_MAX_SECONDS", "60"))
    # Max researchers to process per ORCID run (0 = all). Researchers never harvested come
    # first, then the least recently harvested, so repeated runs rotate through everyone.
    orcid_researchers_per_run: int = int(os.getenv("ORCID_RESEARCHERS_PER_RUN", "500"))
    scopus_base_url: str = os.getenv(
        "SCOPUS_BASE_URL", "https://api.elsevier.com/content/search/scopus"
    )
    scopus_api_key: str = os.getenv("SCOPUS_API_KEY", "")
    scopus_affiliation_id: str = os.getenv("SCOPUS_AFFILIATION_ID", "60022195")
    wos_base_url: str = os.getenv(
        "WOS_BASE_URL", "https://api.clarivate.com/apis/wos-starter/v1/documents"
    )
    wos_api_key: str = os.getenv("WOS_API_KEY", "")
    wos_organization: str = os.getenv("WOS_ORGANIZATION", "University of Pretoria")
    dspace_endpoint: str = os.getenv(
        "DSPACE_ENDPOINT", "https://repository.up.ac.za/server/oai/request"
    )
    dspace_metadata_prefix: str = os.getenv("DSPACE_METADATA_PREFIX", "oai_dc")
    dspace_set_spec: str = os.getenv("DSPACE_SET_SPEC", "")

    @property
    def mysql_url(self) -> str:
        user = quote_plus(self.mysql_user)
        password = quote_plus(self.mysql_password)
        host = self.mysql_host
        return f"mysql+pymysql://{user}:{password}@{host}:{self.mysql_port}/{self.mysql_database}"


settings = Settings()
