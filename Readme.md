# University of Pretoria Research Analytics

This project is a clean full-stack foundation for a researcher analytics tool that harvests records from OpenAlex and ORCID, harmonizes them into a shared schema, removes duplicates across sources, stores the results in MySQL, and exposes a React dashboard through a FastAPI backend.

## Stack

- Backend: Python, FastAPI, SQLAlchemy
- Database: MySQL 8
- Frontend: React with Vite
- Scheduling: APScheduler in a dedicated Docker service
- Runtime: Docker Compose

## What the pipeline does

1. Harvests OpenAlex first for institution `I69552723`, records canonical works, and creates researcher mappings only from OpenAlex authorships that include an ORCID iD.
2. Enriches those OpenAlex-discovered researchers from ORCID public records by researcher ORCID iD. ORCID works merge into existing OpenAlex works by DOI/fallback fingerprint where possible.
3. Normalizes titles, DOIs, authors, affiliations, publishers, and keywords.
4. De-duplicates records across sources using DOI-first matching and a fallback fingerprint.
5. Stores `researchers` (every row has an ORCID iD), `researcher_works` (researcher-to-work links), `source_records` for provenance, `works` for canonical analytics, and `harvest_runs` for run history.
6. Runs daily harvesting automatically inside Docker.

## Quick start

1. Copy the environment template.

```bash
cp .env.example .env
```

2. Edit `.env` and add your OpenAlex and ORCID settings.

3. Start the full stack.

```bash
docker compose up --build
```

4. Open the application.

- Frontend dashboard: `http://localhost:3000`
- Backend API: `http://localhost:8000`
- Health check: `http://localhost:8000/health`

## Docker commands

From the project root:

```bash
docker compose up -d --build
```

Check that the API is healthy:

```bash
curl http://127.0.0.1:8000/health
```

Follow logs:

```bash
docker compose logs -f backend
docker compose logs -f frontend
docker compose logs -f mysql
```

Restart after code changes:

```bash
docker compose up -d --build backend frontend scheduler
```

Stop the stack:

```bash
docker compose down
```

Stop and remove the MySQL Docker volume as well:

```bash
docker compose down -v
```

## Daily automation

The scheduler service runs every day using `HARVEST_SCHEDULE_HOUR` and `HARVEST_SCHEDULE_MINUTE`.
The timezone is fixed to `Africa/Johannesburg`.

If you want a harvest immediately when the stack starts, set:

```env
RUN_HARVEST_ON_STARTUP=true
```

## Daily cron job

If you want a simple OS-level cron job instead of the long-running APScheduler process, use:

```bash
python scripts/harvest_all.py --limit 1000
```

This command harvests the default sources, always in this order:

1. `openalex`
2. `orcid`

**OpenAlex (primary source).** The institution-wide OpenAlex harvest runs for
`OPENALEX_INSTITUTION_ID` and creates canonical works from OpenAlex metadata.
Researcher validation and mapping are ORCID-based: an author is linked or
recorded as a researcher only when the OpenAlex authorship includes an ORCID iD,
and new researcher rows are created only for ORCID-bearing authors affiliated
with the configured institution on that work. Authors without ORCID iDs are not
recorded as researchers.

**ORCID (enrichment).** The ORCID pass uses only the researcher ORCID iDs mapped
from OpenAlex. It fetches each mapped researcher's public ORCID `/works`, merges
those works into the canonical `works` table, and links them through
`researcher_works`. Profiles whose ORCID works list has not changed since the
last run are skipped.

Researchers are processed never-harvested first, then least recently harvested.
`--limit` stops starting new researchers once that many records were fetched,
and `ORCID_RESEARCHERS_PER_RUN` caps researchers per run, so repeated daily
runs rotate through everyone.

For the OpenAlex/ORCID-only rebuild, run:

```bash
python scripts/harvest_all.py --sources openalex orcid
```

Example Linux cron entry for a daily run at 02:00:

```cron
0 2 * * * cd /path/to/analytics-research && /path/to/venv/bin/python scripts/harvest_all.py --limit 1000 >> /var/log/up-research-harvest.log 2>&1
```

If you only want selected sources:

```bash
python scripts/harvest_all.py --limit 1000 --sources openalex orcid
```

## ORCID configuration

ORCID harvesting uses the Public API by default. Add Public API credentials to
`.env` so the harvester can request a `/read-public` token using the
client-credentials OAuth flow:

```env
ORCID_CLIENT_ID=your-orcid-client-id
ORCID_CLIENT_SECRET=your-orcid-client-secret
```

In the default OpenAlex-first rebuild, ORCID is used for enrichment of the
researcher ORCID iDs already mapped from OpenAlex. `ORCID_RESEARCHERS_PER_RUN=0`
processes every mapped researcher on each run.

If you already have a long-lived `/read-public` token, set `ORCID_ACCESS_TOKEN`
instead. For verified ORCID sign-in, also set `ORCID_REDIRECT_URI`; the backend
then exposes `/api/auth/orcid/login` and `/api/auth/orcid/callback`.

For the first clean rebuild after truncating the database, use a larger limit
and reset checkpoints:

```bash
python scripts/harvest_all.py --sources openalex orcid --limit 10000 --reset-state
```

If OpenAlex returns `429 Too Many Requests`, set a contact email and slow the
request pace in `.env`, then rerun without deleting the database:

```env
OPENALEX_API_KEY=your-free-openalex-api-key
OPENALEX_MAILTO=your.email@example.com
OPENALEX_PER_PAGE=50
OPENALEX_REQUEST_DELAY_SECONDS=1
OPENALEX_MAX_ATTEMPTS=12
OPENALEX_RETRY_AFTER_MAX_SECONDS=60
OPENALEX_TITLE_ENRICHMENT_LIMIT=0
```

## Local backend run without Docker

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Run the scheduler separately if needed:

```bash
python scheduler.py
```

## Frontend development

```bash
cd frontend
npm install
npm run dev
```

## Important notes

- The API keys you shared were intentionally not written into tracked source files.
- Scopus and Web of Science response structures can differ by tenant and entitlement, so validate live field mappings with your actual credentials.
- The current de-duplication logic is DOI-first, with a fallback hash based on normalized title, year, first author, and journal.
- If you are still seeing only a few hundred records, increase the per-source limit in `scripts/harvest_all.py` or pass a larger `--limit`, and make sure your real Scopus and Web of Science API keys are in `.env`.

## One-time duplicate merge maintenance

If historic duplicate `works` already exist in the database, run the maintenance script:

```bash
python scripts/merge_duplicate_works.py
```

This is a dry-run by default and prints what would be merged.

To apply changes:

```bash
python scripts/merge_duplicate_works.py --apply
```

The routine clusters duplicates by:

- DOI (preferred)
- fallback normalized `title + publication_year`

Then it merges metadata, re-links source records to the primary work, and deletes redundant duplicate work rows.

## Reset DSpace incremental checkpoint

If you need to force a full DSpace backfill from scratch, reset only the DSpace harvest state:

```bash
python scripts/reset_dspace_state.py
```

This runs as a dry-run and shows the current DSpace state.

To apply:

```bash
python scripts/reset_dspace_state.py --apply
```

After resetting, run:

```bash
python scripts/harvest_all.py --sources dspace --limit 0
```
