#!/bin/sh
# Put the career-lens customer key in job-finder-config/.env, the env file of job-finder.
# It reads the key from ../plans/v1.key, never prints it, and keeps the other lines of the file.
# It also makes a dashboard password when the file has none. It never changes a password that exists.
set -eu
cd "$(dirname "$0")"
umask 077
mkdir -p job-finder-config
file=job-finder-config/.env
touch "$file"
grep -v '^CAREER_LENS_API_KEY=' "$file" > "$file.new" || true
printf 'CAREER_LENS_API_KEY=%s\n' "$(cat ../plans/v1.key)" >> "$file.new"
if ! grep -q '^JOB_FINDER_PASSWORD=.' "$file.new"; then
  grep -v '^JOB_FINDER_PASSWORD=' "$file.new" > "$file.tmp" || true
  mv "$file.tmp" "$file.new"
  printf 'JOB_FINDER_PASSWORD=%s\n' "$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')" >> "$file.new"
fi
mv "$file.new" "$file"
echo "Wrote $file. Next: docker compose run --rm jobfinder job-finder login"
