#!/bin/sh
set -eu
umask 077
cd "$(dirname "$0")/.."
mkdir -p backups
backup_dir=$(mktemp -d "./backups/lodge-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
restart_app() {
    docker compose start web worker
}
trap restart_app EXIT
docker compose stop web worker
docker compose exec -T db pg_dump -U lodge -d lodge --format=custom > "$backup_dir/database.dump"
docker compose run --rm -T --no-deps --entrypoint tar web -czf - -C /app/media . > "$backup_dir/uploads.tar.gz"
printf '%s\n' "Database and uploads backup complete." > "$backup_dir/COMPLETE"
printf 'Completed backup: %s\n' "$backup_dir"
