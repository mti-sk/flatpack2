# flatpack2

**CLI + Web-GUI controller for Eltek Flatpack2 48V/2000W HE rectifiers via CAN bus**

> Tested on real hardware with Waveshare USB-CAN-A adapter (STM32, CH341).

---

## ⚠️ Disclaimer

This project is tested and works on real hardware, but it is **experimental
software based on reverse engineering** of an undocumented CAN protocol.
It is provided **as-is, with no warranty, and no liability for any damage**
resulting from its use – including damage to your PSU, battery, or other
connected equipment. Not affiliated with Eltek. You use this software
entirely at your own risk.

Full text: [`DISCLAIMER.md`](DISCLAIMER.md)

---

## Features

- **CAN communication** via Waveshare USB-CAN-A (native binary protocol, not slcan)
- **Auto-detection** of adapter by USB VID:PID `1a86:7523`
- **Virtual PTY terminal** – connect with `screen /tmp/flatpack2.pty` from any SSH session
- **Web-GUI dashboard** – mobile-first dark theme, live SSE updates every 10 s
  - PSU status (Vout, Iout, Vin, temperatures, mode)
  - Set voltage / current with confirmation dialog
  - **Standby button** – sets PSU to 48.0 V / 0.1 A safe idle state
  - LiFePO4 charger control (start / stop / progress bar)
  - **Battery parameters card** – static config overview (cells, voltages, currents, limits)
  - Live graphs: Vout+Iout, Delivered capacity [Ah], Delivered energy [Wh] – time window 15 min / 1 h / 12 h
  - CC→CV transition annotation in graph
  - Live log tail
  - CSV history export (`/api/history`)
- **LiFePO4 CC/CV charger** with soft-start and safe battery detection
  - DETECT phase: PSU holds low `detect_voltage` at minimal `detect_current`; battery detected via voltage rise on output (no inrush current)
  - RAMP phase: voltage stepped up from V_bat to target in configurable steps; avoids inrush spikes on connection
  - CC phase: constant current until target voltage reached
  - CV phase: voltage held, current tapers until `charge_current_tail`
  - Battery disconnect during RAMP returns to DETECT automatically
  - **Auto-start** on program startup and after CAN reconnect (configurable)
- **Standby mode** – sets PSU to 48.0 V / 0.1 A; overwrites restore values so reconnect keeps standby
- **Value restore on reconnect** – last set voltage/current automatically re-applied after CAN bus recovery
- **Charge resume on reconnect** – if charging was active before CAN loss, it resumes automatically
- **Daemonization** – double-fork, PID file, systemd service included
- **CAN watchdog** – auto-reconnect on communication loss
- **Per-PSU startup config** – serial number mapping, auto-apply on discovery
- **Configurable power rating** – `power_rating` (1800/2000/3000 W) derives enforced current/power limits for your specific PSU variant
- **Log rotation** – `RotatingFileHandler`
- **SIGHUP** reloads log level from the config file specified at startup (`--config`)

---

## Hardware

| Component | Details |
|-----------|---------| 
| PSU | Eltek Flatpack2 48V / 2000W HE |
| Adapter | Waveshare USB-CAN-A (Model A, STM32 + CH341) |
| CAN speed | 125 kbit/s |
| Serial baudrate | 2 000 000 baud |
| USB VID:PID | `1a86:7523` |

### CAN protocol (confirmed by hardware testing)

| Frame | Arbitration ID | Notes |
|-------|---------------|-------|
| LOGIN TX | `0x05004804` | Keepalive every 1 s; PSU times out after 15 s |
| STATUS RX | `(arb & 0xFFFFFF00) == 0x05014000` | 8-byte status frame |
| SET TX | `0x05FF4004` | Broadcast – only this address works |
| ALERT | `0x0501BFFC` | Alert request/response |

**SET data format:** `struct.pack("<HHHH", iout_da, vout_cv, vout_cv, ovp_cv)`
- `iout_da` = current × 10 (deciamps)
- `vout_cv` = voltage × 100 (centivolts)
- `ovp_cv`  = OVP voltage × 100

**Frame format (Waveshare binary protocol):**
```
AA  (E0|len)  [ID 4 bytes LE]  [data 0–8 bytes]  55
```

---

## Requirements

- Python 3.8+
- Linux (uses `/dev/ttyUSB*`, sysfs, pty)
- User must be in `dialout` group (or run as root)

```bash
pip install -r requirements.txt
# pyserial>=3.5
# flask>=3.0
```

---

## Installation

### Option A – pipx (recommended, isolated environment)

```bash
# Install pipx if not already installed
sudo apt install pipx
pipx ensurepath

# Install flatpack2 directly from GitHub
pipx install git+https://github.com/mti-sk/flatpack2.git

# Run
flatpack2 --config /etc/flatpack2/flatpack2.conf
```

To upgrade to the latest version:

```bash
pipx upgrade flatpack2
```

To uninstall:

```bash
pipx uninstall flatpack2
```

---

### Option B – install.sh (venv + systemd, automated)

```bash
# Clone the repository
git clone https://github.com/mti-sk/flatpack2.git
cd flatpack2

# Install (PSU controller only)
sudo bash install.sh

# Or install with LiFePO4 charger config
sudo bash install.sh --config flatpack2_charger.conf
```

The script will:
- Create a Python virtual environment at 
- Install dependencies (pyserial, flask)
- Copy program files to 
- Copy config to  (only if not already present)
- Create wrapper 
- Install and start systemd service
- Add current user to  group

**Upgrade** – run the script again; existing config is preserved:

```bash
sudo bash install.sh
```

**Uninstall:**

```bash
# Keep config files
sudo bash uninstall.sh

# Remove everything including config
sudo bash uninstall.sh --purge
```

---

### Option C – manual (foreground / development)

```bash
git clone https://github.com/mti-sk/flatpack2.git
cd flatpack2

# Create venv and install dependencies
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Add user to dialout (logout/login required)
sudo usermod -aG dialout $USER

# Run in foreground
python3 flatpack2.py
screen /tmp/flatpack2.pty
```

### Using charger configuration

```bash
python3 flatpack2.py --config flatpack2_charger.conf
```

### systemd service (manual setup)

```bash
sudo cp flatpack2.service /etc/systemd/system/
sudo nano /etc/systemd/system/flatpack2.service   # adjust paths if needed
sudo systemctl daemon-reload
sudo systemctl enable flatpack2
sudo systemctl start flatpack2
sudo systemctl status flatpack2
journalctl -u flatpack2 -f
```

---

## Configuration

Two config files are provided:

| File | Purpose |
|------|---------| 
| `flatpack2.conf` | PSU controller only, charger section commented out |
| `flatpack2_charger.conf` | Same as above + LiFePO4 charger enabled |

Specify config file with `--config`:
```bash
python3 flatpack2.py --config flatpack2_charger.conf
```

### Key sections

```ini
[can]
channel          = /dev/ttyUSB0   # tried first; autodetect fallback
bitrate          = 125000         # Flatpack2 CAN speed
serial_baudrate  = 2000000        # Waveshare adapter serial speed
autodetect       = true           # find adapter by USB VID:PID

[psu]
ovp_voltage      = 60.0           # over-voltage protection (V)
discovery_timeout = 10            # seconds to wait for PSU on startup
power_rating     = 2000           # PSU power variant (W): 1800 / 2000 / 3000
                                   # derives enforced I_MAX = power_rating / 48.0V

[webgui]
enabled          = true
host             = 0.0.0.0        # 127.0.0.1 for localhost only
port             = 8080
log_access       = true

[daemon]
enabled          = false
pidfile          = /var/run/flatpack2.pid

[terminal]
type             = pty
pty_symlink      = /tmp/flatpack2.pty
```

### Per-PSU startup configuration

```ini
[PSU_1]
serial           = 173350049492   # serial number (from 'map' command)
voltage          = 54.0           # V
current          = 20.0           # A
apply_on_start   = true           # apply after stable status detected
                                  # ignored when charger auto_start = true
```

Serial number is matched against the hex bytes reported by the PSU hello frame.
If `serial` is omitted, mapping is by discovery order (first seen = PSU_1).

### LiFePO4 charger configuration

```ini
[charger]
cell_count           = 16       # cells in series
cell_voltage_max     = 3.60     # V per cell (CV target = cell_count × cell_voltage_max)
capacity             = 400      # Ah (informational)
charge_current       = 35.0     # A (CC phase)
charge_current_tail  = 4.0      # A (end-of-charge detection in CV phase)
safety_time_limit    = 1200     # minutes (20 h) – counted from RAMP start
detect_voltage       = 48.0     # V – PSU output during DETECT phase
detect_current       = 0.2      # A – PSU current during DETECT phase
detect_threshold     = 1.0      # V – vout must exceed detect_voltage + detect_threshold
min_current_detect   = 0.5      # A – secondary confirmation: iout threshold after detection
ramp_step_voltage    = 0.1      # V per ramp step
ramp_step_interval   = 5.0      # seconds between ramp steps
voltage_tolerance    = 0.1      # V – CC→CV transition threshold
monitor_interval     = 5.0      # seconds between monitor refreshes
auto_start           = true     # start charging automatically on program start
                                # and after CAN reconnect (default: true)
```

#### Charger phases

| Phase | Description |
|-------|-------------|
| **detect** | PSU holds `detect_voltage` at `detect_current`. Battery detected when `vout > detect_voltage + detect_threshold`. No inrush current. |
| **ramp** | Voltage steps up from V_bat by `ramp_step_voltage` every `ramp_step_interval` s. Transitions to CC when `iout >= charge_current`, or to CV when target reached. Battery disconnect returns to detect. |
| **CC** | Full `charge_current` applied. Ends when `vout >= target_voltage - voltage_tolerance`. |
| **CV** | Target voltage held. Current tapers naturally. |
| **done** | Charging finished when `iout <= charge_current_tail`. |
| **error** | Stopped due to alarm, high temp, or safety timeout. |

#### Auto-start behaviour

When `auto_start = true` (default):
- Charging starts automatically after PSU discovery on program startup.
- After a CAN bus reconnect, charging resumes automatically **only if it was active** before the disconnection. If the user stopped charging manually before the outage, it will **not** restart.
- When auto-start is active, `apply_on_start` in `[PSU_x]` sections is effectively superseded by the charger.

---

## Standby mode

Standby sets the PSU to a safe low-power idle state:

| Parameter | Value |
|-----------|-------|
| Voltage | 48.0 V |
| Current | 0.1 A (PSU minimum) |

Standby **overwrites** the restore values, so after a CAN bus outage the PSU will return to standby rather than to a previously set charging voltage. This is intentional – use `charge start` or `set` to resume normal operation after standby.

---

## Usage

### Foreground with PTY terminal

```bash
# Terminal 1 – start program
python3 flatpack2.py

# Terminal 2 – connect to PTY
screen /tmp/flatpack2.pty
```

### Daemon mode

```bash
python3 flatpack2.py --daemon
screen /tmp/flatpack2.pty     # connect to running daemon
```

### Web-GUI

Open in browser: `http://<host>:8080`

Default is `0.0.0.0:8080` – accessible from any device on the local network.
For localhost-only access set `host = 127.0.0.1` in `[webgui]`.

---

## CLI Commands

Connect via PTY (`screen /tmp/flatpack2.pty`) or use stdio mode.

```
help                    Show help
get                     Show status of all PSUs
get <id>                Show status of PSU with given ID
set <id|all> <V> <I>    Set voltage (V) and current (A)
standby                 Set ALL PSUs to standby (48.0 V / 0.1 A)
standby <id>            Set PSU <id> to standby
map                     Show serial → ID mapping

charge start [I]        Start LiFePO4 charging (optional current override)
charge stop             Stop charging
charge status           Show charge status
charge monitor          Continuous monitor (Enter to stop)
charge config           Show charger configuration
charge battery          Show battery parameters (static config)

shutdown                Stop program
```

**Examples:**
```
get
get 1
set all 54.0 20.0
set 1 48.0 10.0
standby
standby 1
map
charge start
charge start 15.0
charge status
charge battery
charge monitor
```

---

## Web-GUI API

All endpoints return JSON unless noted.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Dashboard HTML |
| GET | `/events` | SSE stream (10 s interval) |
| GET | `/api/status` | Single status snapshot (JSON) |
| POST | `/api/set` | Set voltage/current |
| POST | `/api/standby` | Set PSU to standby (48.0 V / 0.1 A) |
| POST | `/api/charge/start` | Start charging |
| POST | `/api/charge/stop` | Stop charging |
| GET | `/api/history` | Download history as CSV |

**POST /api/set**
```json
{ "voltage": 54.0, "current": 20.0 }
```

**POST /api/charge/start**
```json
{ "current": 15.0 }   // optional; omit to use config default
```

**POST /api/standby**
```json
{}   // no body required
```

**GET /api/history**
```
?window=900    // seconds; omit for full 12h history
```
Returns CSV: `timestamp_ms,vout,iout,ah,wh`

---

## PSU Limits (Flatpack2 48V family)

The Eltek Flatpack2 48V rectifier is produced in several power variants.
The voltage range and CAN protocol are identical across variants; only the
max current/power differs. Set `power_rating` in `[psu]` to match your
hardware – this drives the software-enforced current/power limits.

| `power_rating` | Voltage | Current (I_MAX) | Power |
|----------------|---------|------------------|-------|
| 1800 W         | 43.5 – 57.6 V | ~37.5 A | 1800 W |
| **2000 W** (default) | 43.5 – 57.6 V | ~41.7 A | 2000 W |
| 3000 W         | 43.5 – 57.6 V | ~62.5 A | 3000 W |

`I_MAX` is derived as `power_rating / 48.0 V` (nominal). Current is
automatically limited if `V × I` would exceed the configured power.

**Note:** setting `power_rating` only changes the software limit check – it
does not change what your physical unit is actually rated for. Make sure
the value matches the label on your PSU; see [`DISCLAIMER.md`](DISCLAIMER.md).

---

## Signals

| Signal | Action |
|--------|--------|
| `SIGTERM` | Graceful shutdown |
| `SIGHUP` | Reload log level from config (uses `--config` path from startup) |

---

## File Structure

```
flatpack2.py             Main program
flatpack2.conf           Configuration – PSU controller only
flatpack2_charger.conf   Configuration – PSU controller + LiFePO4 charger
flatpack2.service        systemd service unit
requirements.txt         Python dependencies
README.md                This file
DISCLAIMER.md            Legal disclaimer (no warranty, no liability, experimental software)
```

---

## Session state summary (for continuation)

**Version:** 2.9.4

**Hardware confirmed working:**
- CAN frame format (AA / E0|len / ID LE / data / 55)
- LOGIN arb `0x05004804`, interval 1 s
- SET arb `0x05FF4004` (broadcast only)
- STATUS mask `0xFFFFFF00 == 0x05014000`
- Serial number format: hex bytes reported as hex string
- Startup apply via broadcast SET works correctly on real hardware
- Value restore after CAN reconnect confirmed working

**Known issues:**
- Multi-PSU STATUS dispatch maps all STATUS to PSU_1 – cannot fix without hardware testing; `yy` byte role needs verification
- DETECT→RAMP→CC charger flow untested on real hardware
- Assumption: `vout` in STATUS frame reflects actual output terminal voltage even when PSU is passive (battery backfeed). Needs hardware verification.
- `power_rating` variants (1800W/3000W) are only validated logically (limit math); only the 2000W variant has been confirmed on real hardware so far

**v2.9.4 changes:**
- Added `[psu] power_rating` config option (1800/2000/3000W, default 2000W) – derives I_MAX/P_MAX
- Added `DISCLAIMER.md`, referenced from top of README
- Fixed stale 53.5V standby mentions left over in README after the v2.9.3 change to 48.0V
- Translated v2.9.3 changelog entry to English

**v2.9.3 changes:**
- Bug fix: Ah/Wh not working – missing `now = time.time()` in `on_status`
- Bug fix: battery detection – added OR condition (voltage-based + current-based)
- Standby values: 53.5V → 48.0V
- `charge stop` / DONE / ERROR → PSU set to 48.0V / 0.1A
- RAMP starts from `vout - 3 × ramp_step_voltage` (not exactly from vout)
- Web GUI Charger: Actual V, Actual I, RAMP progress bar, fixed phases
- Alert request bug during CC status – noted, fix deferred
