#!/usr/bin/env bash
set -euo pipefail
# Запуск из корня проекта. Останавливаем запись на время согласованного снимка БД и файлов.
backup_dir="${1:?Usage: scripts/backup.sh /absolute/path/to/backup-directory}"
mkdir -p "$backup_dir"
backup_dir="$(cd "$backup_dir" && pwd)"
docker compose stop api worker
trap 'docker compose start api worker >/dev/null' EXIT
docker compose exec -T mysql sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysqldump -uroot --single-transaction --routines --triggers --no-tablespaces ama' | gzip > "$backup_dir/mysql.sql.gz"
docker compose run --rm --no-deps --user 0:0 -v "$backup_dir:/backup" --entrypoint sh api -c 'tar -czf /backup/files.tar.gz -C /data files'
sha256sum "$backup_dir/mysql.sql.gz" "$backup_dir/files.tar.gz" > "$backup_dir/SHA256SUMS"
