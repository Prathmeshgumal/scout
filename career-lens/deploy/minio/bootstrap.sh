#!/bin/sh
set -eu

: "${ROOT_USER:?missing MinIO root user}"
: "${ROOT_PASSWORD:?missing MinIO root password}"
: "${SERVICE_ACCESS_KEY:?missing artifact service access key}"
: "${SERVICE_SECRET_KEY:?missing artifact service secret key}"
: "${BUCKET:?missing artifact bucket}"

if [ "$ROOT_USER" = "$SERVICE_ACCESS_KEY" ]; then
  echo "root and artifact access keys must differ" >&2
  exit 1
fi

case "$BUCKET" in
  *[!a-z0-9.-]*)
    echo "BUCKET contains unsupported characters" >&2
    exit 1
    ;;
esac

while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    *'@CAREER_LENS_BUCKET@'*)
      prefix=${line%%@CAREER_LENS_BUCKET@*}
      suffix=${line#*@CAREER_LENS_BUCKET@}
      printf '%s%s%s\n' "$prefix" "$BUCKET" "$suffix"
      ;;
    *) printf '%s\n' "$line" ;;
  esac
done < /artifact-policy.json.template > /tmp/artifact-policy.json

mc alias set local http://minio:9000 "$ROOT_USER" "$ROOT_PASSWORD"
mc mb --ignore-existing "local/$BUCKET"
mc admin user add local "$SERVICE_ACCESS_KEY" "$SERVICE_SECRET_KEY"
mc admin policy create local career-lens-artifacts /tmp/artifact-policy.json
mc admin policy attach local career-lens-artifacts --user "$SERVICE_ACCESS_KEY"
