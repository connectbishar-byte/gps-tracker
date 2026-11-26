#!/usr/bin/env bash
# Deploy a new version of the code (run from the unpacked new release): sudo bash deploy/hostinger/update.sh
set -euo pipefail
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
APP=/srv/meltice-gps-tracker/app; VENV=/srv/meltice-gps-tracker/venv; ENVF=/etc/meltice-gps-tracker/fleet.env
bash "$SRC/deploy/hostinger/backup.sh"
rsync -a --delete --exclude ".env" --exclude "data/" --exclude "logs/" --exclude "staticfiles/" --exclude "__pycache__/" "$SRC/" "$APP/"
chown -R fleet:fleet "$APP"
run() { sudo -u fleet bash -c "set -a; source $ENVF; set +a; cd $APP; $VENV/bin/python manage.py $*"; }
sudo -u fleet "$VENV/bin/pip" install -q -r "$APP/requirements.txt"
run migrate --noinput
run createcachetable
run collectstatic --noinput -v0
install -m 644 "$APP/deploy/hostinger/fleet-purge.service" /etc/systemd/system/fleet-purge.service
install -m 644 "$APP/deploy/hostinger/fleet-purge.timer" /etc/systemd/system/fleet-purge.timer
systemctl daemon-reload
systemctl enable --now fleet-purge.timer
systemctl restart gunicorn-fleet fleet-receiver
systemctl --no-pager status gunicorn-fleet fleet-receiver | grep -E "Active"
