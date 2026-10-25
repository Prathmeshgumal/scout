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
  -d '{"url":"https://www.linkedin.com/company/example/","linkedin_sections":["about","jobs"]}'
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
curl -H "$AUTH" "$API/v1/google-search?query=python+jobs+bangalore"
curl -H "$AUTH" $API/v1/google-search/$ID
curl -H "$AUTH" "$API/v1/url?url=https://example.com/"
curl -H "$AUTH" $API/v1/url/$ID

# Search and read in one flow
curl -H "$AUTH" -H 'Content-Type: application/json' $API/v1/search -d '{"query":"python jobs"}'
curl -H "$AUTH" $API/v1/search/$ID
```

Company job rows are in the `jobs` section of `/v1/linkedin-company`.

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
