# career-lens

A small scraping service. It reads LinkedIn people and companies, runs Google
searches and captures single pages, using a real Chrome on your own machine.
Results are kept as an append-only history and served as JSON.

Each scrape goes request → capture → observation. A section reports `partial`,
`absent`, `not_captured` or `failed` separately, so "nothing there" and "did not
look" never look the same.

## Setup

You need Docker, [uv](https://docs.astral.sh/uv/) and Google Chrome on a desktop
session, because the workers run a visible browser.

```bash
uv sync
uv run python scripts/init_local_env.py
docker compose up -d --build
curl --fail http://127.0.0.1:8090/healthz
docker compose exec -T api career-lens api-key create me
```

Keep the printed key. It is shown once.

Start a worker in its own terminal:

```bash
cp deploy/supervisor.example.json deploy/supervisor.json
uv run patchright install chrome
uv run career-lens supervisor --env-file .env --config deploy/supervisor.json
```

To keep the native worker running, install it as a systemd user service. It starts when you log in and restarts after a crash.

```bash
systemctl --user enable --now "$PWD/deploy/career-lens-worker.service"
journalctl --user -u career-lens-worker -f
```

## The console

Open <http://127.0.0.1:8090/console> and paste the key. The key lives in the
page's memory only, so reloading the page asks for it again.

- **Workers**: which workers are live and healthy, and the Google switch and
  budget. Turn Google on for a worker before searching.
- **LinkedIn accounts**: add an account, then assign it to a worker. A Chrome
  window opens on that worker to sign in. If LinkedIn asks for a code, enter it
  in that window. The state moves to `ready` when the login is verified.
- **Working hours**: LinkedIn scrapes run only inside an account's hours, so an
  account needs hours before it does any work.
- **Try it**: submit a search or a scrape, then watch **Recent requests**. A
  queued request shows why it is waiting.

## API

Every route needs `Authorization: Bearer <key>`, except `/healthz`, `/console`,
`/docs`, `/redoc` and `/openapi.json`. The examples use:

```bash
API=http://127.0.0.1:8090
AUTH="Authorization: Bearer $CAREER_LENS_KEY"
```

```bash
# Health
curl $API/healthz

# Submit scrapes. Each one returns a request id.
curl -H "$AUTH" -H 'Content-Type: application/json' $API/v1/scrape/linkedin-company \
  -d '{"url":"https://www.linkedin.com/company/example/","linkedin_sections":["about","jobs"],"job_pages":5}'
curl -H "$AUTH" -H 'Content-Type: application/json' $API/v1/scrape/linkedin-job \
  -d '{"url":"https://www.linkedin.com/jobs/view/1234567890/"}'
curl -H "$AUTH" -H 'Content-Type: application/json' $API/v1/scrape/linkedin-job-search \
  -d '{"keywords":"backend engineer","location":"Bengaluru","posted_within":"week","workplace_types":["remote","hybrid"],"job_pages":5}'
curl -H "$AUTH" -H 'Content-Type: application/json' $API/v1/scrape/linkedin-individual \
  -d '{"url":"https://www.linkedin.com/in/example/"}'
curl -H "$AUTH" -H 'Content-Type: application/json' $API/v1/scrape/google-search \
  -d '{"query":"python jobs bangalore"}'
curl -H "$AUTH" -H 'Content-Type: application/json' $API/v1/scrape/url \
  -d '{"url":"https://example.com/"}'

# Follow or cancel a request
curl -H "$AUTH" $API/v1/requests/$ID
curl -H "$AUTH" -X POST $API/v1/requests/$ID/cancel

# Read results
curl -H "$AUTH" "$API/v1/linkedin-company?url=https://www.linkedin.com/company/example/"
curl -H "$AUTH" "$API/v1/linkedin-individual?url=https://www.linkedin.com/in/example/"
curl -H "$AUTH" "$API/v1/linkedin-job?url=https://www.linkedin.com/jobs/view/1234567890/"
curl -H "$AUTH" $API/v1/linkedin-job-search/$ID
curl -H "$AUTH" "$API/v1/google-search?query=python+jobs+bangalore"
curl -H "$AUTH" $API/v1/google-search/$ID
curl -H "$AUTH" "$API/v1/url?url=https://example.com/"
curl -H "$AUTH" $API/v1/url/$ID

# Search and read in one flow
curl -H "$AUTH" -H 'Content-Type: application/json' $API/v1/search -d '{"query":"python jobs"}'
curl -H "$AUTH" $API/v1/search/$ID
```

Company job rows are in the `jobs` section of `/v1/linkedin-company`.

Job pages, job postings and job searches run signed in, on a LinkedIn account,
so they wait for the account's working hours and pacing.

- `job_pages` is how many pages of job results to read, about 25 jobs each:
  1 to 20, default 5. It works for a company's jobs and for a job search.
  `reached_end: false` means more jobs exist past the last page read.
- `/v1/linkedin-job` returns one posting with its full `description_text`,
  `posting_status` (`open` or `closed`), location, workplace and employment type.
- `/v1/linkedin-job-search/{id}` returns 202 while running. A failed search has
  `status: failed`, a `failure` and `result: null`, never an empty job list.
  `posted_within` is `day`, `week` or `month`; `workplace_types` takes
  `on_site`, `remote` and `hybrid`. LinkedIn ignores a separate location, so
  the search opens as "<keywords> in <location>". A search that matches
  nothing succeeds with `items: []` and `reached_end: true`.

## Develop

```bash
docker compose up -d postgres minio createbucket
uv run ruff check src tests scripts
uv run pytest -q
```

Tests use a real Postgres and MinIO, and never contact a live third-party site.

## Stopping

`docker compose down` keeps your data. `docker compose down -v` deletes the
database and every capture in this project.
