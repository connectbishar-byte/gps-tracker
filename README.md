# Meltice GPS Tracker — Phase 1 vehicle GPS tracking prototype

Receives Teltonika **FMC130** telemetry over cellular TCP (Codec 8 / Codec 8 Extended), keeps every
raw packet, decodes GPS and I/O values, stores them, and shows live and historical data in a web UI.
Built to the *Meltice GPS Tracker Phase 1 Scope* and the *FMC130 Connectivity Runbook*.

```
FMC130 ──cellular──▶ TCP receiver :5027 ──▶ CRC + codec validation ──▶ raw packet + decoded records
                     (asyncio, separate          (one DB transaction, ACK only after commit)
                      process)                                   │
Browser ◀── Django web (Bootstrap + Leaflet, polls every 5 s) ◀──┘  SQLite (local) / SQL Server or Postgres (VPS)
```

## What is in the box

| Area | Where |
|---|---|
| Codec 8 / 8E parser, CRC-16/IBM, framing, encoder | `tracking/codec.py` |
| Async TCP receiver (IMEI allow-list, 0x01/0x00 handshake, fragmented + coalesced frames, timeouts, connection & rate limits, heartbeat) | `tracking/receiver.py` |
| Ingest: raw + records in one transaction, ACK after commit, idempotent replay, late-arrival flag, newest-fix-wins | `tracking/ingest.py` |
| FMC130 AVL I/O dictionary (versioned, unknown IDs kept) | `tracking/io_dictionary.py` |
| Data model: Device, Connection, RawPacket, AVLRecord, IOValue, ReceiverHeartbeat, TestRun | `tracking/models.py` |
| Screens: Dashboard, Live map, History, Decoded records, I/O inspector, Raw data (restricted + CSV export with masked IMEI), Devices/diagnostics, Connectivity, Settings | `tracking/templates/` |
| Device simulator (multi-vehicle, Codec 8/8E, no-fix injection) | `manage.py simulate_device` |
| Connectivity / acceptance suite (13 protocol checks) | `manage.py conntest`, web **Connectivity → Run test**, `tools/fmc_probe.py` |
| Automated tests (27: official Codec samples, bad CRC, truncation, fragmentation, replay, DB failure → no ACK, web auth) | `tracking/tests/` |
| Local hosting with Docker | `docker-compose.yml`, `Dockerfile` |
| Hostinger VPS hosting (systemd + Nginx, or Docker) | `deploy/hostinger/`, `docs/HOSTINGER_DEPLOYMENT.md` |
| Physical FMC130 bench test guide | `docs/DEVICE_BENCH_TEST.md` |
| Acceptance matrix → evidence | `docs/ACCEPTANCE_TESTS.md` |

---

## Option A — Local hosting

### A1. Docker (recommended, any OS with Docker Desktop)

```bash
cp .env.example .env            # set FLEET_ADMIN_PASSWORD, and FLEET_SEED_DEMO=true for demo vehicles
docker compose up -d --build
docker compose --profile sim up -d simulator     # optional: 3 simulated vehicles driving in Abu Dhabi
```

* Web UI: <http://127.0.0.1:8000> (login `admin` / the password from `.env`)
* Tracker TCP port: `5027` on all interfaces of your PC
* Logs: `docker compose logs -f receiver`
* Stop: `docker compose --profile sim down` (data stays in the `fleetdata` volume; add `-v` to wipe it)

### A2. Plain Python (Windows / macOS / Linux, Python 3.10+)

```powershell
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env            # macOS/Linux: cp; then edit FLEET_ADMIN_PASSWORD
python manage.py migrate
python manage.py ensure_admin
python manage.py seed_demo        # optional demo vehicles + 2 h of history
```

Then run the two processes in **separate terminals** (they are deliberately independent):

```powershell
python manage.py run_receiver --host 0.0.0.0 --port 5027
python manage.py runserver 127.0.0.1:8000
```

On Windows, `tools\start_local.ps1` opens both for you. Check the listener with
`Get-NetTCPConnection -LocalPort 5027 -State Listen` and `Test-NetConnection -ComputerName 127.0.0.1 -Port 5027`.

### Test connectivity locally

```bash
python manage.py conntest                       # 13 protocol acceptance checks against 127.0.0.1:5027
python manage.py simulate_device --interval 5   # one simulated FMC130 (Ctrl+C to stop)
python manage.py simulate_device --imei 356307042441021,356307042441054 --codec 8 --no-fix-every 10
python manage.py test tracking                  # unit + end-to-end tests
```

Or open **Connectivity → Run test** in the web UI. To connect the real FMC130 to your PC you need a
public TCP path (router port-forward or raw-TCP tunnel) — see `docs/DEVICE_BENCH_TEST.md`.

---

## Option B — Hostinger hosting

Requires a **Hostinger VPS** (KVM, Ubuntu 22.04/24.04). Shared / Cloud web hosting cannot run a raw
TCP listener on port 5027, so it cannot receive tracker data.

```bash
# on the VPS, from the unpacked folder
sudo bash deploy/hostinger/install.sh
sudo certbot --nginx -d fleet.melticetech.com
sudo ufw allow 5027/tcp      # only if UFW is active — and open 5027 in hPanel's VPS firewall
```

Result: web at `https://fleet.melticetech.com` (Nginx → gunicorn `127.0.0.1:9040`), tracker endpoint
`gps.melticetech.com:5027`, services `gunicorn-fleet` and `fleet-receiver`. Full walkthrough including
DNS, SQL Server, Docker alternative, verification and rollback: **`docs/HOSTINGER_DEPLOYMENT.md`**.

Prove the VPS is reachable from the internet (run on your PC, not on the VPS):

```bash
python tools/fmc_probe.py gps.melticetech.com 5027
```

---

## Configuration

All settings are environment variables (`.env` locally, `/etc/meltice-gps-tracker/fleet.env` on Hostinger);
see `.env.example`. Key ones: `FLEET_DB_ENGINE` (`sqlite`|`mssql`|`postgres`|`mysql`),
`FLEET_PUBLIC_GPS_HOST/PORT` (what the device is configured with), `FLEET_MAX_CONNECTIONS`,
`FLEET_CONNECTS_PER_MINUTE_PER_IP`, `FLEET_READ_TIMEOUT`, `FLEET_STALE_AFTER_SECONDS`.

## Users and access

* Everything requires login. Staff users can register devices and run self-tests.
* **Raw data** is visible only to staff or members of the `raw_data` group (create it in `/admin/`).
* Raw CSV export masks IMEIs (`3563*******1013`).
* The IMEI allow-list identifies devices; it is **not** authentication. Use carrier restrictions /
  a controlled test network, and evaluate device TLS before production.

## Behaviour notes (from the scope)

* ACK (4-byte record count) is sent **only after** raw packet and records are committed. A DB
  failure closes the link with no ACK, so the device keeps and resends its data.
* Bad CRC / malformed frames are stored (bounded) with the error and answered with ACK 0.
* Replays are idempotent: `(device, record fingerprint)` is unique; the arrival is still logged.
* Records whose device time is older than the newest seen are flagged **late**; they never move the
  map pin backwards. Records without a GNSS fix (0 satellites or 0/0) are stored but never plotted.
* Timestamps are stored in UTC and shown in UAE time (`Asia/Dubai`).
* Fuel, RPM, temperature etc. are only shown if the device actually sends them — nothing is fabricated.

## Not in Phase 1

Multi-tenancy, billing, mobile apps, remote immobilisation, Codec 12 commands, CAN/OBD, AI features
(geofences, trips, driver behaviour, alerts — roadmap).
