#!/usr/bin/env bash
# Meltice GPS Tracker - native install on a Hostinger VPS (Ubuntu 22.04/24.04).
# Run from the unpacked project folder:   sudo bash deploy/hostinger/install.sh
#
# What it does (idempotent, safe to re-run):
#   - creates service account 'fleet', /srv/meltice-gps-tracker, /var/lib/meltice-gps-tracker, /var/log/meltice-gps-tracker
#   - copies the app, creates a virtualenv, installs requirements
#   - creates /etc/meltice-gps-tracker/fleet.env (only if missing) with a random secret key
#   - runs migrations, collectstatic, creates the admin user
#   - installs fleet-receiver.service + gunicorn-fleet.service
#   - installs a NEW Nginx server block for fleet.melticetech.com (backs up /etc/nginx first, runs nginx -t)
# What it does NOT do (do these consciously, see docs/HOSTINGER_DEPLOYMENT.md):
#   - enable/reset UFW, change other Nginx sites, request TLS certificates, open the Hostinger panel firewall
set -euo pipefail

SRC="$(cd "$(dirname "$0")/../.." && pwd)"
APP=/srv/meltice-gps-tracker/app
VENV=/srv/meltice-gps-tracker/venv
ENVF=/etc/meltice-gps-tracker/fleet.env
WEB_DOMAIN="${WEB_DOMAIN:-fleet.melticetech.com}"
GPS_DOMAIN="${GPS_DOMAIN:-gps.melticetech.com}"
DB_EXTRA="${DB_EXTRA:-}"          # set DB_EXTRA=mssql or postgres to install that driver set

[[ $EUID -eq 0 ]] || { echo "Run with sudo"; exit 1; }
echo "==> Pre-flight: ports already in use (5027 / 9040)"
if [[ -f /etc/systemd/system/fleet-receiver.service ]]; then
  echo "   existing Meltice GPS Tracker install detected - upgrading in place"
elif command -v ss >/dev/null && ss -ltnp | grep -E ":(5027|9040)\b"; then
  echo "!! Port 5027 or 9040 is used by another program - stop and investigate before continuing."; exit 1
else
  echo "   free"
fi

echo "==> Packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip nginx sqlite3 rsync iproute2 sudo >/dev/null

echo "==> Service account and folders"
id fleet >/dev/null 2>&1 || useradd --system --home /srv/meltice-gps-tracker --shell /usr/sbin/nologin fleet
install -d -o fleet -g fleet -m 750 /srv/meltice-gps-tracker /var/lib/meltice-gps-tracker /var/log/meltice-gps-tracker
install -d -o root -g fleet -m 750 /etc/meltice-gps-tracker

echo "==> Application code -> $APP"
rsync -a --delete --exclude ".env" --exclude "data/" --exclude "logs/" --exclude "staticfiles/" --exclude "__pycache__/" "$SRC/" "$APP/"
chown -R fleet:fleet "$APP"

echo "==> Virtualenv"
[[ -d $VENV ]] || sudo -u fleet python3 -m venv "$VENV"
REQ=requirements.txt
[[ $DB_EXTRA == mssql ]] && REQ=requirements-mssql.txt
[[ $DB_EXTRA == postgres ]] && REQ=requirements-postgres.txt
sudo -u fleet "$VENV/bin/pip" install -q --upgrade pip
sudo -u fleet "$VENV/bin/pip" install -q -r "$APP/$REQ"

if [[ ! -f $ENVF ]]; then
  echo "==> Creating $ENVF"
  SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(50))")
  ADMINPW=$(python3 -c "import secrets; print(secrets.token_urlsafe(14))")
  sed -e "s|^FLEET_SECRET_KEY=.*|FLEET_SECRET_KEY=$SECRET|" \
      -e "s|^FLEET_ADMIN_PASSWORD=.*|FLEET_ADMIN_PASSWORD=$ADMINPW|" \
      -e "s|^FLEET_ENVIRONMENT_LABEL=.*|FLEET_ENVIRONMENT_LABEL=Hostinger|" \
      -e "s|^FLEET_DEBUG=.*|FLEET_DEBUG=false|" \
      -e "s|^FLEET_ALLOWED_HOSTS=.*|FLEET_ALLOWED_HOSTS=$WEB_DOMAIN,127.0.0.1,localhost|" \
      -e "s|^# FLEET_CSRF_TRUSTED_ORIGINS=.*|FLEET_CSRF_TRUSTED_ORIGINS=https://$WEB_DOMAIN|" \
      -e "s|^# FLEET_BEHIND_HTTPS_PROXY=.*|FLEET_BEHIND_HTTPS_PROXY=true|" \
      -e "s|^# FLEET_SQLITE_PATH=.*|FLEET_SQLITE_PATH=/var/lib/meltice-gps-tracker/fleet.sqlite3|" \
      -e "s|^FLEET_PUBLIC_GPS_HOST=.*|FLEET_PUBLIC_GPS_HOST=$GPS_DOMAIN|" \
      "$APP/.env.example" > "$ENVF"
  echo "FLEET_LOG_DIR=/var/log/meltice-gps-tracker" >> "$ENVF"
  chown root:fleet "$ENVF"; chmod 640 "$ENVF"
  echo "   Admin user: admin   password: $ADMINPW   (also stored in $ENVF - change it after first login)"
else
  echo "==> Keeping existing $ENVF"
fi

echo "==> Migrate / static / admin"
run() { sudo -u fleet bash -c "set -a; source $ENVF; set +a; cd $APP; $VENV/bin/python manage.py $*"; }
run migrate --noinput
run collectstatic --noinput -v0
run ensure_admin
# self-test IMEI so tools/fmc_probe.py and the web self-test work immediately (disable it later in Devices)
run register_device 356307042441013 Self-test-simulator --model SIMULATOR --group Test
run check

echo "==> systemd units"
install -m 644 "$APP/deploy/hostinger/fleet-receiver.service" /etc/systemd/system/fleet-receiver.service
install -m 644 "$APP/deploy/hostinger/gunicorn-fleet.service" /etc/systemd/system/gunicorn-fleet.service
systemctl daemon-reload
systemctl enable --now gunicorn-fleet fleet-receiver
systemctl restart gunicorn-fleet fleet-receiver

echo "==> Nginx server block for $WEB_DOMAIN"
SITE=/etc/nginx/sites-available/$WEB_DOMAIN
if [[ ! -f $SITE ]]; then
  tar czf "/root/nginx-backup-$(date +%Y%m%d-%H%M%S).tgz" /etc/nginx
  sed "s/fleet.melticetech.com/$WEB_DOMAIN/g" "$APP/deploy/hostinger/nginx-fleet.melticetech.com.conf" > "$SITE"
  ln -sf "$SITE" "/etc/nginx/sites-enabled/$WEB_DOMAIN"
  if nginx -t; then systemctl reload nginx; else echo "!! nginx -t failed - removing new site"; rm -f "/etc/nginx/sites-enabled/$WEB_DOMAIN" "$SITE"; exit 1; fi
else
  echo "   $SITE exists - left unchanged"
fi

echo "==> Firewall (not changed automatically)"
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  echo "   UFW is active. To accept tracker traffic run:  sudo ufw allow 5027/tcp"
else
  echo "   UFW inactive. Do NOT enable it before allowing SSH (sudo ufw allow OpenSSH)."
fi
echo "   Also add TCP 5027 (and 80/443) in hPanel -> VPS -> Security -> Firewall if that firewall is enabled."

sleep 3
systemctl --no-pager --lines=3 status fleet-receiver gunicorn-fleet | grep -E "Active|Loaded" || true
cat <<MSG

Done.
  Web (after DNS + TLS):  https://$WEB_DOMAIN      (now: curl -I http://127.0.0.1:9040/login/)
  Tracker endpoint:       $GPS_DOMAIN:5027  (TCP, DNS-only record)
  Next:  sudo certbot --nginx -d $WEB_DOMAIN
         sudo -u fleet bash -c 'cd $APP && $VENV/bin/python tools/fmc_probe.py 127.0.0.1 5027 --quick'
MSG
