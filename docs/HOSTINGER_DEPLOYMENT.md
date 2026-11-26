# Hostinger deployment (Option B)

Target: `https://fleet.melticetech.com` for the web UI and `gps.melticetech.com:5027/TCP` for the FMC130.

> **Plan requirement:** a Hostinger **VPS** (KVM 1 or larger, Ubuntu 22.04/24.04). Shared/Cloud/WordPress
> hosting only serves HTTP(S) and cannot accept the tracker's raw TCP connection on port 5027.

Two ways to run it on the VPS — pick one:

| | B1. Native (systemd) — matches the scope | B2. Docker on the VPS |
|---|---|---|
| Services | `fleet-receiver.service`, `gunicorn-fleet.service` | `web` + `receiver` containers |
| Web bind | `127.0.0.1:9040` behind Nginx | `127.0.0.1:9040` behind Nginx |
| Install | `sudo bash deploy/hostinger/install.sh` | `docker compose -f docker-compose.yml -f deploy/hostinger/docker-compose.hostinger.yml up -d --build` |

---

## 0. Before you change anything (runbook §7)

```bash
ssh root@<VPS-IP>
sudo tar czf /root/nginx-backup-$(date +%F).tgz /etc/nginx     # back up Nginx
sudo ss -ltnp                                                 # inventory listening ports
sudo ss -ltnp | grep -E ":(5027|9040)\b"                      # must print nothing
sudo ufw status verbose
```

Note which other sites run on this VPS — you will re-check them at the end.

## 1. DNS (Hostinger hPanel → Domains → DNS / or Cloudflare)

| Type | Name | Value | Proxy |
|---|---|---|---|
| A | `fleet` | VPS public IPv4 | may be proxied (HTTP) |
| A | `gps` | VPS public IPv4 | **DNS only** (grey cloud) — Cloudflare's HTTP proxy cannot carry Teltonika TCP |

Check from your PC: `nslookup gps.melticetech.com` → VPS IP.

## 2. Upload the code

From your PC: `scp meltice-gps-tracker.zip root@<VPS-IP>:/root/` then on the VPS:

```bash
apt-get install -y unzip && cd /root && unzip -o meltice-gps-tracker.zip && cd meltice-gps-tracker
```

(or `git clone` once the code is in GitHub.)

## 3a. Install — native (B1)

```bash
sudo bash deploy/hostinger/install.sh
# optional overrides: WEB_DOMAIN=fleet.example.com GPS_DOMAIN=gps.example.com DB_EXTRA=mssql sudo -E bash deploy/hostinger/install.sh
```

The script prints the generated **admin password** — save it. It creates:

* service account `fleet`, code in `/srv/meltice-gps-tracker/app`, virtualenv `/srv/meltice-gps-tracker/venv`
* config `/etc/meltice-gps-tracker/fleet.env` (root:fleet, 640) with a random `FLEET_SECRET_KEY`
* SQLite DB `/var/lib/meltice-gps-tracker/fleet.sqlite3`, logs `/var/log/meltice-gps-tracker/{web,receiver}.log`
* systemd units `gunicorn-fleet` (127.0.0.1:9040) and `fleet-receiver` (0.0.0.0:5027)
* a **new** Nginx site for `fleet.melticetech.com` only (other sites untouched; `nginx -t` before reload)
* the self-test IMEI `356307042441013` on the allow-list (disable it in *Devices* after testing)

It does **not** touch UFW, TLS or the Hostinger panel firewall — do those in step 4.

Re-running is safe (keeps `fleet.env` and data). Upgrades: unpack the new release and run
`sudo bash deploy/hostinger/update.sh` (it backs up first).

## 3b. Install — Docker (B2)

```bash
curl -fsSL https://get.docker.com | sh            # if Docker is not installed (Hostinger also has a Docker VPS template)
cp .env.example .env && nano .env                 # set FLEET_SECRET_KEY, FLEET_ADMIN_PASSWORD, FLEET_PUBLIC_GPS_HOST=gps.melticetech.com
docker compose -f docker-compose.yml -f deploy/hostinger/docker-compose.hostinger.yml up -d --build
sudo cp deploy/hostinger/nginx-fleet.melticetech.com.conf /etc/nginx/sites-available/fleet.melticetech.com
sudo ln -s /etc/nginx/sites-available/fleet.melticetech.com /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

## 4. HTTPS and firewalls

```bash
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d fleet.melticetech.com          # obtains cert and adds HTTP→HTTPS redirect

# UFW — only if it is already active. Never enable UFW before allowing SSH.
sudo ufw allow 5027/tcp
sudo ufw allow 'Nginx Full'
```

**hPanel → VPS → Security → Firewall:** if a firewall is attached, add *Accept TCP 5027* (and 80/443).
Where the SIM operator's egress range is known, restrict 5027 to that source range.
Never expose the database port publicly.

## 5. Verify

On the VPS:

```bash
sudo systemctl status fleet-receiver gunicorn-fleet --no-pager
sudo ss -ltnp | grep -E ":(5027|9040)\b"         # 0.0.0.0:5027 python, 127.0.0.1:9040 gunicorn
curl -I http://127.0.0.1:9040/login/              # 200
sudo -u fleet /srv/meltice-gps-tracker/venv/bin/python /srv/meltice-gps-tracker/app/tools/fmc_probe.py 127.0.0.1 5027
sudo journalctl -u fleet-receiver -f              # live receiver log
```

From your PC (different network — this is what proves the tracker can reach it):

```powershell
Test-NetConnection gps.melticetech.com -Port 5027
python tools\fmc_probe.py gps.melticetech.com 5027
```

Then log in at `https://fleet.melticetech.com`, open **Connectivity** — the probe's test run and
connections appear there — and **Live map** shows the probe's test point.

Finally re-check every other site/application on the VPS still works (scope: *Regression*).

## 6. Point the FMC130 at Hostinger

In Teltonika Configurator → **GPRS**: Domain `gps.melticetech.com`, Port `5027`, Protocol `TCP`,
ACK `AVL`, TLS `None` (controlled test only). Register the device's IMEI on the **Devices** page first
(or on the VPS: `sudo -u fleet bash -c "set -a; . /etc/meltice-gps-tracker/fleet.env; cd /srv/meltice-gps-tracker/app; /srv/meltice-gps-tracker/venv/bin/python manage.py register_device <IMEI> <Name>"`). Change one destination at a time. See `docs/DEVICE_BENCH_TEST.md`.

Optional packet capture while testing (contains location data — keep it short, delete after):
`sudo tcpdump -ni any tcp port 5027 -c 50`

## 7. Database options

SQLite is the default and is fine for a single-device trial (WAL mode, one writer thread).

**SQL Server (scope's planned VPS database).** Needs ≥ 2 GB RAM if installed on the same VPS, or use an
existing SQL Server reachable privately.

```bash
# Microsoft ODBC driver 18 (Ubuntu) — follow https://learn.microsoft.com/sql/connect/odbc/linux-mac/installing-the-microsoft-odbc-driver-for-sql-server
sudo ACCEPT_EULA=Y apt-get install -y msodbcsql18 unixodbc-dev
sudo -u fleet /srv/meltice-gps-tracker/venv/bin/pip install -r /srv/meltice-gps-tracker/app/requirements-mssql.txt
```

In SQL Server create database `meltice_gps_tracker` and a least-privilege login (db_owner on that DB only
for migrations). Then in `/etc/meltice-gps-tracker/fleet.env`:

```
FLEET_DB_ENGINE=mssql
FLEET_DB_NAME=meltice_gps_tracker
FLEET_DB_USER=fleet_app
FLEET_DB_PASSWORD=...
FLEET_DB_HOST=127.0.0.1
FLEET_DB_PORT=1433
```

and run `migrate`, then restart both services. Keep port 1433 closed to the internet. Confirm the
installed `mssql-django` release supports the Django version in `requirements.txt` before switching;
this prototype was verified end-to-end on SQLite. PostgreSQL works the same way
(`requirements-postgres.txt`, `FLEET_DB_ENGINE=postgres`).

## 8. Operations

* Backups: `sudo crontab -e` → `15 2 * * * bash /srv/meltice-gps-tracker/app/deploy/hostinger/backup.sh`
  (SQLite online backup + env, 14-day retention in `/var/backups/meltice-gps-tracker`).
* Logs rotate automatically (5 × 5 MB per process) and also go to `journalctl`.
* Change the admin password after first login (`/admin/` → Users).

## 9. Rollback

```bash
sudo systemctl disable --now fleet-receiver gunicorn-fleet
sudo rm /etc/nginx/sites-enabled/fleet.melticetech.com && sudo nginx -t && sudo systemctl reload nginx
sudo ufw delete allow 5027/tcp          # if you added it
# remove the gps/fleet DNS records if no longer needed; restore the device's previous Configurator file
```

Data stays in `/var/lib/meltice-gps-tracker` until you delete it.

## Troubleshooting (local works, VPS fails)

| Check | Command |
|---|---|
| Listener up? | `sudo ss -ltnp \| grep 5027` |
| Service errors? | `sudo journalctl -u fleet-receiver -n 100` |
| UFW / hPanel firewall | `sudo ufw status`, hPanel firewall rules |
| DNS-only for `gps`? | `nslookup gps.melticetech.com` must return the VPS IP, not a Cloudflare IP |
| Reachable from outside? | `python tools/fmc_probe.py gps.melticetech.com 5027` from a non-VPS network |
| IMEI accepted? | *Connectivity* page → rejected handshakes |
