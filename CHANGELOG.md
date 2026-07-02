# Changelog

All notable changes to flatpack2 are documented in this file.

## [2.9.5] - 2026
### Added
- **Bundled data files in the wheel**: `flatpack2.conf`, `flatpack2_charger.conf`,
  `flatpack2.service`, `install.sh` and `uninstall.sh` are now shipped inside
  the pip/pipx package (installed to `<venv>/share/flatpack2/`)
- **`flatpack2 --files`**: prints the location of the bundled files and
  ready-to-paste commands for copying the config to `/etc/flatpack2/` and the
  systemd unit to `/etc/systemd/system/` (with the correct `ExecStart=` path);
  works for pipx/venv installs, `pip install --user` and git checkouts

### Fixed
- **`pyproject.toml`: invalid build backend** `setuptools.backends.legacy:build`
  (module does not exist) replaced with `setuptools.build_meta`; this fixes
  `pipx install git+...` (Option A in README), which previously failed to build
- **Charger ERROR phase was never reported**: `_do_stop()` always set phase to
  `done`, so safety timeout / PSU ALARM / High Temp stops showed as `done`
  instead of `error` in CLI and Web GUI. PSU behaviour (standby on stop) is
  unchanged; only the reported phase is corrected
- **SIGHUP log-level reload now also updates handler levels** - previously
  raising verbosity (e.g. INFO -> DEBUG) at runtime had no effect because the
  file/console handlers kept their original level
- **Concurrent CAN reconnect race**: `_reconnect()` could be triggered
  simultaneously by the rx thread (SerialException) and the watchdog; a
  non-blocking guard lock now ensures only one reconnect loop runs at a time
- **Shipped `flatpack2.service` was broken for manual installs**: used system
  `python3` (flask typically missing), `Type=forking` + `--daemon`, a foreign
  `Documentation=` URL, and `PrivateTmp=true` which hid `/tmp/flatpack2.pty`
  in a private namespace so `screen` could not attach. The unit now matches
  the layout produced by `install.sh` (venv python, `Type=simple`, journal
  output, PrivateTmp disabled with an explanatory comment)

### Changed
- Removed stale AI-session notes: module docstring line ("charger ...
  untested on hardware") and the README "Session state summary" section,
  replaced by a proper **Known limitations** chapter (documents the
  single-PSU STATUS dispatch limitation, `vout` backfeed assumption,
  power-variant validation status, and Web-GUI security notes)
- Removed dead code: `Charger.start_monitor()`, `Charger._monitor_thread`,
  `Charger._running` (CLI `charge monitor` has its own implementation)
- Minor cleanups: module-level `datetime` reused in `FlatpackBus._print()`
  (no per-call import), dashboard `<html lang>` fixed to `en`, bare
  `except:` in `/api/charge/start` narrowed to `(ValueError, TypeError)`
- Version bump 2.9.4 -> 2.9.5

---

## [2.9.4] - 2026
### Added
- **`[psu] power_rating`** (1800/2000/3000 W, default 2000) – derives enforced
  I_MAX/P_MAX limits (`power_rating / 48.0V nominal`) for the actual PSU
  power variant; voltage range/OVP are unaffected (fixed by the 48V platform)
- **`DISCLAIMER.md`** – legal disclaimer (experimental/reverse-engineered
  software, no warranty, no liability); summary added to top of `README.md`

### Fixed
- **README/CHANGELOG inconsistency**: several places still documented the
  old standby values (53.5V) after they were changed to 48.0V in v2.9.3
- CLI help text and Web-GUI voltage/current input limits are now generated
  from the loaded config instead of hardcoded 2000W-variant defaults

### Changed
- Last changelog entry (v2.9.3) translated from Czech to English
- Version bump 2.9.3 → 2.9.4

---


### Fixed
- **Bug: Ah/Wh integration not working** – missing `now = time.time()` in `on_status` caused a `NameError` and zero Ah/Wh values during CC/CV phase
- **Bug: battery detection was voltage-only** – added an OR condition: `vout > detect_v + threshold OR iout >= min_current_detect`; now also covers a battery discharged below `detect_voltage`

### Changed
- **Standby values: 53.5V → 48.0V** (current 0.1A unchanged)
- **`charge stop` / DONE / ERROR** – PSU is always set to standby values (48.0V / 0.1A)
- **RAMP start** – voltage now starts from `max(vout - 3 × ramp_step_voltage, detect_voltage)` instead of exactly from `vout`; guarantees the first SET is below battery voltage
- **Web GUI Charger card** – added Actual V and Actual I values (live from SSE)
- **Web GUI Charger progress bar** – now shows progress during RAMP phase too (`ramp_v / target_v × 100`)
- **Web GUI phase map** – fixed/added `detect`, `ramp` phases; removed the old `waiting` phase
- **SSE payload** – added `ramp_v` field to charger data
- Version bump 2.9.2 → 2.9.3

---

## [2.9.2] - 2026
### Changed
- **Charger: replaced WAITING phase with DETECT + RAMP soft-start**
  - DETECT phase: PSU holds configurable `detect_voltage` (default 48V) at `detect_current` (default 0.2A)
  - Battery detected when `vout > detect_voltage + detect_threshold` (voltage-based, no inrush)
  - RAMP phase: voltage steps up by `ramp_step_voltage` (0.1V) every `ramp_step_interval` (5s) from V_bat to target
  - Battery disconnect during RAMP resets to DETECT, clears Ah/Wh counters
  - Safety timer counts from RAMP start (not from program start)
- **Config: removed `start_current` and `battery_detect_delay`**
- **Config: added `detect_voltage`, `detect_current`, `detect_threshold`, `ramp_step_voltage`, `ramp_step_interval`**
- Web GUI: new DETECT and RAMP phase badges; battery card updated with new parameters
- CLI: `charge config` and `charge battery` updated with new parameters
- Version bump 2.9.1_en → 2.9.2

---
## [2.9.1_en] - 2026
### Changed
- Full English translation of all user-facing text:
  - Web GUI dashboard labels, buttons, confirmation dialogs, status texts
  - Chart dataset labels
  - API JSON response messages
  - CLI charger output messages
  - Python source code comments
  - Charger config file comments
- Version bump 2.9.0 → 2.9.1_en

---

## [2.9.0] - 2026
### Added
- `pyproject.toml` – pip/pipx packaging with entry point `flatpack2`
- `install.sh` – automated venv installation, systemd setup, dialout group, upgrade support
- `uninstall.sh` – clean removal (`--purge` flag also removes config)
- `LICENSE` – MIT licence
- `.gitignore` – Python, venv, logs, IDE files
- `CHANGELOG.md` – this file

### Changed
- `README.md` – added installation sections: pipx from GitHub, install.sh, uninstall

---

## [2.8.2] - 2026
### Changed
- **Web GUI:** Standby button moved into same row as Nastavit button
- **Web GUI:** Battery parameters card expanded to span 2 grid columns on desktop; internal two-column layout
- **Web GUI:** Mode badge added to header (Zdroj / Nabíječka – \<phase\>), updated via SSE
- Version bump 2.8.0 → 2.8.2

---

## [2.8.0] - 2026
### Added
- **Standby mode:** CLI `standby [id]`, Web GUI button, API `POST /api/standby`
  - Sets PSU to 53.5 V / 0.1 A, overwrites restore values
- **Charger auto-start:** `auto_start = true` in `[charger]` config section (default: true)
  - Charging starts automatically after PSU discovery on program startup
- **Charge resume after CAN reconnect:** resumes only if charging was active before outage
- **Web GUI:** Battery parameters card (static config, shown only when charger configured)
- **CLI:** `charge battery` command – shows static battery config
- **SIGHUP fix:** now reloads from `--config` path used at startup (was hardcoded to `flatpack2.conf`)

---

## [2.7.5] - 2026
### Added
- Initial public release
- CAN communication via Waveshare USB-CAN-A (native binary protocol)
- Auto-detection of adapter by USB VID:PID `1a86:7523`
- Virtual PTY terminal (`screen /tmp/flatpack2.pty`)
- Web-GUI dashboard (Flask, SSE, mobile-first dark theme)
  - PSU status, set voltage/current, live graphs (Vout+Iout, Ah, Wh)
  - CC→CV transition annotation, live log tail, CSV history export
- LiFePO4 CC/CV charger (WAITING → CC → CV → DONE/ERROR)
- Value restore on CAN reconnect
- Daemonization (double-fork, PID file)
- CAN watchdog with auto-reconnect
- Per-PSU startup config (serial number mapping, apply_on_start)
- Log rotation (RotatingFileHandler)
- systemd service unit
- SIGTERM graceful shutdown, SIGHUP log level reload
