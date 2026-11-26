# Physical FMC130 bench test

Follows the *FMC130 Connectivity Runbook*. Do Stage A (simulator, `manage.py conntest` all PASS) before
connecting hardware.

## 1. Prepare the device

1. Power off, remove the internal battery connector, insert the data-enabled Micro-SIM, reconnect.
   Use the official first-start wiring with a fused 10–30 V supply. Do not guess pin assignments.
2. Connect Micro-USB, open Teltonika Configurator, **Read** the device, then **Save to file** the
   original configuration (rollback copy).
3. Record IMEI, firmware, SIM operator/APN and the configuration file name.
4. Register the IMEI in the web UI → **Devices** (name, model FMC130, firmware, config revision).

## 2. Configurator settings (bench)

| Tab | Setting | Value |
|---|---|---|
| GPRS | Context / APN | Enable; operator APN + credentials |
| GPRS | Domain / Port | Local: your public IP or TCP-tunnel host / port. Hostinger: `gps.melticetech.com` / `5027` |
| GPRS | Protocol / ACK | TCP / AVL |
| GPRS | TLS / second server | None (controlled test only) / Disabled |
| GPRS | Records | Open link 300 s, response 30 s, oldest first |
| System | Data protocol | Codec 8 Extended (Codec 8 also accepted) |
| System | Sleep mode | Disabled during powered bench test (restore afterwards) |
| Data acquisition | Stopped | min period 30 s, send period 10 s |
| Data acquisition | Moving | min period 10 s, send period 10 s, 100 m / 10° / 10 km/h |
| I/O | Enable (low priority, monitoring) | 239 Ignition, 240 Movement, 21 GSM signal, 69 GNSS status, 181/182 PDOP/HDOP, 66 External voltage, 67/68 Battery V/A, 200 Sleep mode, 16 Total odometer |

**Save to device**, read back, and export a dated configuration file.

## 3. Reaching a local PC (Option A with real hardware)

A tracker on a cellular network cannot reach `127.0.0.1` or a private LAN IP. Choose one:

* **Router port-forward** (needs a public IPv4, not CGNAT): forward TCP 5027 → your PC's LAN IP:5027,
  and allow it in Windows Firewall:
  `New-NetFirewallRule -DisplayName "Meltice FMC130 5027" -Direction Inbound -Protocol TCP -LocalPort 5027 -Action Allow`
  If your router's WAN IP differs from <https://ifconfig.me>, you are behind CGNAT — forwarding will not work.
* **Raw TCP tunnel** (e.g. a service that offers TCP, not just HTTP, endpoints). The device is configured
  with the tunnel's hostname and **assigned port**. HTTP-only tunnels cannot carry the binary protocol.
* **Skip local hardware** and point the device at the Hostinger VPS instead.

Prove reachability **from a different internet connection** (phone hotspot):
`python tools/fmc_probe.py <public-host> <port>` — a local `Test-NetConnection` cannot prove cellular reachability.

Set `FLEET_PUBLIC_GPS_HOST/PORT` in `.env` so the UI shows the endpoint the device uses.

## 4. Observe

| Where | What to see |
|---|---|
| Connectivity → Recent connections | device's remote IP, handshake `accepted`, frames counting up |
| Connectivity → rejected rows | IMEI typo / device not registered |
| Raw data | packets with CRC OK, codec 8E, ACK `sent (n)` |
| I/O inspector | ignition, movement, GSM, GNSS status, voltages; unknown IDs highlighted |
| Live map | pin within ~5 s of the commit once there is an outdoor GNSS fix; indoors you will see "no valid fix" |
| `logs/receiver.log` or `journalctl -u fleet-receiver -f` | `IMEI …: n records (n new, 0 dup) ACK n` |

## 5. Outage test

1. With data flowing, disconnect the network (unplug the antenna/SIM data, or stop the receiver).
2. Let the device buffer for a few minutes, restore.
3. Expect: device reconnects, buffered records arrive with older device time → marked **late** in
   History/Records; the map pin does not jump back; no duplicates (`new / dup` columns on Raw data).
4. DB-failure behaviour (no false ACK) is proven by the automated test
   `test_db_failure_means_no_ack`; on a bench you can set `FLEET_FAULT_INJECT_IMEIS=<IMEI>` and restart
   the receiver — frames are not acknowledged and the device resends after removing it.

## 6. Evidence to capture

Configuration export, masked IMEI, firmware, APN/operator, endpoint, UTC test time, raw HEX with
CRC/ACK status (Raw data → CSV export), screenshots of Live map / I/O inspector, the Connectivity test
run. Then restore the original configuration from backup.
