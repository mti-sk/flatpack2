# Changelog

All notable changes to flatpack2 are documented in this file.

## [3.0.2] - 2026
### Fixed
- **PSU overload at the RAMP->CC transition** (diagnosed from a real
  incident's CSV history). On entering CC the charger sent the final
  `target_voltage` in a SINGLE step - e.g. a 54.4V -> 57.6V jump into a
  400Ah battery (~45 mOhm path). The PSU's current limiter cannot follow
  such a step instantaneously and overshoots: measured 42.6A transient on
  a 41.7A/2000W unit (~2.3 kW), after which the PSU tripped its protection
  and shut down. The setpoint now NEVER moves by more than
  `ramp_step_voltage` at a time: after the CC transition the ramp thread
  keeps stepping the setpoint toward `target_voltage`
  (`_cc_setpoint_ramp`); the PSU stays in current limit and each small
  step is absorbed without overshoot. The CC->CV condition is unaffected
  (still `vout >= target - tolerance`, handled by `on_status`); if CV
  triggers while the setpoint is within tolerance of target, the final
  value is sent as a harmless <= tolerance step. This supersedes the
  3.0.0 "RAMP->CC never reached target voltage" fix, which traded the
  stall for the overload.
- **`charge current <A>` in CC phase had the same jump bug** - it re-sent
  `target_voltage` regardless of where the setpoint actually was. It now
  uses the current setpoint (`ramp_voltage`) while the CC setpoint ramp is
  still in progress.

## [3.0.1] - 2026
### Fixed
- **Web-GUI showed no values at all ("--") and buttons were dead** - the
  `chargeReset()` confirm dialog (added in 3.0.0) contained a `\n` escape
  inside the dashboard template. Python interpreted it as a literal newline
  inside a JavaScript string literal, producing a SyntaxError that prevented
  the ENTIRE dashboard script from executing: no SSE connection, no event
  listeners, no charts. Backend/API were unaffected (`/api/status` worked).
  Fixed by escaping to `\\n`.
- **CSV history load could desync the RAM ring buffers** - a truncated last
  row in the history CSV (e.g. power loss during a buffered flush) was
  partially appended: `ts`/`vout` made it into the deques before the parse
  error, leaving the five series with mismatched lengths. All fields are now
  validated before any append, and invalid rows are also dropped from the
  retention rewrite, so the file self-heals on startup.

### Changed
- **Warnings and errors no longer interleave with the CLI prompt.** The PTY
  terminal now has a persistent status bar on the top row (ANSI DECSTBM
  scroll region; normal output scrolls below it). Async events - PSU
  ALARM/WARNING alerts, CAN bus loss/reconnect, charger error stops - are
  shown there (color-coded: red=error, yellow=warning, cyan=info, with a
  timestamp) instead of being printed into the scrollback, which used to
  corrupt the line being typed. In stdio mode the previous inline printing
  is kept as a fallback. New `FlatpackBus.notify(msg, level)` is the single
  entry point (status bar + log + Web-GUI log buffer).
- **Alert notifications are edge-triggered** - the PSU repeats alert
  responses every second (e.g. "Current Limit" for the whole CC phase);
  the user is now notified only when the set of active warnings/alarms
  changes. The status bar keeps the last message visible, and every
  occurrence is still written to the log file.

## [3.0.0] - 2026
### Added
- **Change charge current at runtime** – `charge current <A>` in the CLI and a
  "Current A" input with a **Set I** button in the Web-GUI charger card. Takes
  effect immediately: in RAMP/CC/CV the SET frame is re-sent with the current
  voltage setpoint; in DETECT the new value is stored and applied once the
  battery is detected. New value is range-checked against the PSU limit.
- **Charger reset** – `charge reset` (CLI) and a **Reset** button (Web-GUI,
  with confirmation dialog). Clears the Ah/Wh counters, restores the charge
  current to the config default and restarts the cycle from the DETECT phase.
- **CSV history persistence** – new `[history]` section. When `persist = true`,
  samples are appended to a CSV file (`timestamp_ms,vout,iout,ah,wh`), buffered
  and flushed every `flush_interval` seconds to spare SD/flash media. On startup
  the last 12 h are loaded back into RAM so the graphs survive a restart, and
  rows older than `retention_days` are trimmed (atomic rewrite via temp file).
- **SOC estimate** – rough state-of-charge shown in `charge status`, the SSE
  payload and as the Web-GUI progress bar. Anchored from the resting OCV at
  battery detection (LiFePO4 per-cell OCV→SOC table, linear interpolation),
  then tracked by coulomb counting (`charged Ah / capacity`); in CV it is
  pulled toward 100 % as the current tapers. Always labelled "(estimate)".
- **Live values in the header** – Vout·Iout are shown in the top bar at all
  times; Ah·Wh are added when the charger is configured.
- **Stop reason in the Web-GUI** – the charger card shows the last stop reason
  (was already present in the CLI/SSE, now surfaced in the dashboard).
- Config keys: `webgui.sse_interval`, `charger.disconnect_detect_time`,
  and the whole `[history]` section. All optional with safe fallbacks.

### Fixed
- **Battery disconnect not detected in CC phase** – if the battery was
  unplugged during CC the current collapsed to ~0 A but the charger stayed in
  CC forever. It now watches for `iout <= min_current_detect` sustained for
  `disconnect_detect_time` seconds and returns to DETECT to wait for the
  battery to come back. Ah/Wh counters are preserved across the disconnect.
- **Battery disconnect vs end-of-charge in CV phase** – a sudden current
  collapse (e.g. 20 A → 0 A between two STATUS frames) is now treated as a
  disconnect (→ DETECT), while a gradual taper through the tail value is a real
  end-of-charge (→ done). Previously any current at/below tail ended the charge.
- **RAMP→CC never reached target voltage** – on the RAMP→CC transition the
  setpoint stayed at the last ramp voltage, so the PSU sat in current limit
  below target and the CC→CV condition (`vout >= target − tol`) could never be
  met. The final target voltage is now sent on entering CC.
- **ALARM handling now covers all phases** – PSU ALARM is handled centrally in
  the RX status dispatch (edge-triggered), so it also triggers in DETECT/RAMP,
  not only in CC/CV. On ALARM the PSU is put into standby / the charge is
  aborted into the ERROR state, which requires a manual Start/Reset to clear
  (no silent auto-restart).

### Changed
- **Standby values moved inline** next to the Set/Standby buttons in the
  Web-GUI (previously on a separate line below).
- **Ah/Wh are preserved on battery disconnect** (RAMP and CC/CV) instead of
  being reset; they are cleared only by `charge reset`.
- **Default SSE update interval reduced from 10 s to 3 s** (`sse_interval`),
  for a more responsive dashboard while watching the ramp/current. History
  sampling stays tied to the STATUS frames as before.
- Internal: charger detect/ramp threads now carry a generation counter
  (`run_id`) so a reset/restart cleanly supersedes any in-flight thread and two
  loops can never drive the PSU at the same time.

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
