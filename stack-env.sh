#!/bin/sh
# Put the career-lens customer key in job-finder-config/.env, the env file of job-finder.
# It reads the key from ../plans/v1.key, never prints it, and keeps the other lines of the file.
set -eu
cd "$(dirname "$0")"
umask 077
mkdir -p job-finder-config
file=job-finder-config/.env
touch "$file"
grep -v '^CAREER_LENS_API_KEY=' "$file" > "$file.new" || true
printf 'CAREER_LENS_API_KEY=%s\n' "$(cat ../plans/v1.key)" >> "$file.new"
mv "$file.new" "$file"
echo "Wrote $file. Next: docker compose run --rm jobfinder job-finder login"
