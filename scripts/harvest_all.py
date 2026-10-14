from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import init_db
from app.services.pipeline import IngestionPipeline


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Harvest OpenAlex first as the primary source, record only researchers with "
            "ORCID iDs from OpenAlex authorships, then enrich those mapped researchers "
            "with public ORCID works. DSpace, Scopus and Web of Science can still be "
            "requested explicitly with --sources."
        )
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help=(
            "Approximate record budget per source for this run. OpenAlex/ORCID stop starting "
            "new researchers once it is reached. Use 0 for no limit/full harvest."
        ),
    )
    parser.add_argument(
        "--article-only",
        action="store_true",
        help="Only save article-like records. By default all harvested records are saved.",
    )
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help="Reset saved harvest checkpoints for the selected sources before running.",
    )
    parser.add_argument(
        "--sources",
        nargs="+",
        default=None,
        help=(
            "Optional subset of sources to harvest. By default this runs: openalex orcid. "
            "OpenAlex always runs before ORCID. OpenAlex adds institutional works and creates "
            "researcher mappings only when an authorship has an ORCID iD; ORCID then enriches "
            "those mapped researchers with public ORCID works."
        ),
    )
    parser.add_argument(
        "--title-enrichment-limit",
        "--dspace-enrichment-limit",
        dest="title_enrichment_limit",
        type=int,
        default=None,
        help=(
            "Maximum ORCID/DSpace works without an OpenAlex DOI match to look up individually "
            "by title. Use 0 to skip this slow phase. Defaults to OPENALEX_TITLE_ENRICHMENT_LIMIT."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    init_db()

    pipeline = IngestionPipeline()
    selected_sources = args.sources or pipeline.default_sources()

    logger.info("Starting harvest for sources: %s", ", ".join(selected_sources))
    logger.info("Per-source limit: %s", args.limit)
    logger.info("Article-only filter: %s", args.article_only)
    logger.info("Reset harvest state: %s", args.reset_state)
    if args.reset_state and args.limit > 0:
        logger.warning(
            "--reset-state with --limit %s restarts from the OLDEST OpenAlex works and the "
            "first ORCID researchers every run, so repeated runs re-harvest the same records "
            "and totals will not grow. Run without --reset-state to continue where the last "
            "run stopped.",
            args.limit,
        )
    logger.info(
        "OpenAlex title enrichment limit: %s",
        "configured default" if args.title_enrichment_limit is None else args.title_enrichment_limit,
    )

    try:
        summary = pipeline.ingest_sources(
            source_names=selected_sources,
            limit=args.limit,
            article_only=args.article_only,
            reset_state=args.reset_state,
            title_enrichment_limit=args.title_enrichment_limit,
        )
    except Exception:
        logger.exception("Harvest failed")
        return 1

    logger.info("Harvest completed successfully")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
