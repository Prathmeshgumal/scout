#!/bin/sh
set -eu

: "${CAREER_LENS_APP_DB_PASSWORD:?missing application database password}"
: "${CAREER_LENS_MIGRATE_DB_PASSWORD:?missing migration database password}"

psql --set ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set app_password="$CAREER_LENS_APP_DB_PASSWORD" \
  --set migrate_password="$CAREER_LENS_MIGRATE_DB_PASSWORD" <<'SQL'
CREATE ROLE career_lens_migrate LOGIN CREATEDB PASSWORD :'migrate_password';
CREATE ROLE career_lens_app LOGIN PASSWORD :'app_password';

ALTER DATABASE career_lens OWNER TO career_lens_migrate;
GRANT USAGE ON SCHEMA public TO career_lens_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CREATE ON SCHEMA public TO career_lens_migrate;
SQL
