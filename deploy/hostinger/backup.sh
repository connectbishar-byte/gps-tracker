#!/usr/bin/env bash
# SQLite online backup + env file. Add to root crontab:  15 2 * * * bash /srv/meltice-gps-tracker/app/deploy/hostinger/backup.sh
set -euo pipefail
DB=${FLEET_SQLITE_PATH:-/var/lib/meltice-gps-tracker/fleet.sqlite3}
OUT=/var/backups/meltice-gps-tracker; mkdir -p "$OUT"; chmod 700 "$OUT"
TS=$(date +%Y%m%d-%H%M%S)
[[ -f $DB ]] && sqlite3 "$DB" ".backup '$OUT/fleet-$TS.sqlite3'" && gzip "$OUT/fleet-$TS.sqlite3"
cp /etc/meltice-gps-tracker/fleet.env "$OUT/fleet-$TS.env" 2>/dev/null || true
find "$OUT" -type f -mtime +14 -delete
echo "backup written to $OUT"
