#!/bin/sh
# Write .env for docker-compose.yml: the career-lens customer key that job-finder uses.
# It reads the key from ../plans/v1.key and never prints it.
set -eu
cd "$(dirname "$0")"
umask 077
printf 'JOB_FINDER_CAREER_LENS_API_KEY=%s\n' "$(cat ../plans/v1.key)" > .env
mkdir -p job-finder-config
echo "Wrote .env. Next: docker compose run --rm jobfinder job-finder login"
