# Acceptance test matrix → how this prototype proves each line

Status key: **Automated** = covered by `python manage.py test tracking` (27 tests) and/or the 13-step
connectivity suite (`manage.py conntest`, web Connectivity page, `tools/fmc_probe.py`).
**Manual** = needs the physical device / VPS and must be executed and evidenced by Meltice.

| Scope test | Pass condition | How it is covered | Status |
|---|---|---|---|
| Simulator handshake | Known IMEI accepted; unknown rejected; binary ACK verified | `test_unknown_imei_rejected`, `test_disabled_device_rejected`, `test_ack_equals_committed_records`; suite steps "Unknown IMEI rejected", "Registered IMEI accepted" | Automated – passing |
| Protocol | Official Codec 8/8E samples decode; bad CRC, truncated rejected; fragmented/coalesced tested | `test_codec.py` (official wiki frames, CRC, preamble, oversize, count mismatch, unsupported codec, round-trip incl. 8E variable-length I/O); `test_fragmented_and_coalesced`; suite steps for samples, fragmented, coalesced, bad CRC, truncated | Automated – passing |
| Persistence | Raw HEX + all valid records stored; ACK count = committed records | `test_ack_equals_committed_records` (ACK 5 ↔ 5 rows, raw packet ack_status `sent`) | Automated – passing |
| Retry | Replay → no duplicate normalized records; arrival retained | `test_replay_is_idempotent` (1 record, 2 raw packets, duplicate_records = 1) | Automated – passing |
| Late data | Delayed record does not overwrite newer fix | `test_late_record_does_not_overwrite_newer_fix` | Automated – passing |
| Invalid fix | No false position | `test_invalid_fix_not_used_for_map` | Automated – passing |
| Outage (DB) | No false ACK on DB failure | `test_db_failure_means_no_ack` (fault injection → connection closed, no ACK, rollback, error audited) | Automated – passing |
| Security (web) | Unauthorized web access blocked; raw data restricted; IMEI masked in export | `test_login_required`, `test_raw_restricted_and_export_masks_imei` | Automated – passing |
| Local deployment | Receiver + web run independently; logs and DB match | Verified in Docker (3 containers) and plain Python; web self-test 13/13 against `receiver:5027` | Verified in build environment |
| VPS deployment | Both destinations tested independently | `deploy/hostinger/install.sh` verified on a clean Ubuntu 24.04 container: services up, Nginx → 127.0.0.1:9040 200, probe 13/13 | Script verified; **real VPS: Manual** |
| Physical bench | FMC130 connects and sends; GNSS fix outdoors | `docs/DEVICE_BENCH_TEST.md` | **Manual** |
| Live view | Valid point visible within 5 s of commit | UI polls `/api/fleet/` every 5 s; check with simulator / device | **Manual** (observe) |
| Outage (network) | Buffered data recovered after reconnect | `docs/DEVICE_BENCH_TEST.md` §5 | **Manual** |
| Security (network) | Unknown device denied; no DB public port | Allow-list automated; DB port: SQLite has none, SQL Server must stay private | Partly manual |
| Regression | Existing VPS apps healthy | Installer only adds a new Nginx site and runs `nginx -t`; re-check other sites | **Manual** |

Exit gate (scope §10): agreed tests pass, defects recorded, configuration backup saved, owner signs off.
