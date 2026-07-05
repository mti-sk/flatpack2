#!/usr/bin/env bash
# flatpack2 install / upgrade script
# Copyright (c) 2026 mti@mti.sk | Coded by Claude Sonnet 4.6
# https://github.com/mti-sk/flatpack2
#
# Usage:
#   sudo bash install.sh              # install with default config
#   sudo bash install.sh --config flatpack2_charger.conf   # install with charger config
#
# What this script does:
#   1. Creates /opt/flatpack2/venv  (Python virtual environment)
#   2. Installs dependencies (pyserial, flask)
#   3. Copies program files to /opt/flatpack2/
#   4. Copies config to /etc/flatpack2/ (only if not already present)
#   5. Creates wrapper /usr/local/bin/flatpack2
#   6. Installs systemd service
#   7. Adds current user to dialout group
#
# Upgrade: run the script again - service is stopped, files updated, service restarted.

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
INSTALL_DIR="/opt/flatpack2"
VENV_DIR="${INSTALL_DIR}/venv"
CONFIG_DIR="/etc/flatpack2"
BIN_LINK="/usr/local/bin/flatpack2"
SERVICE_NAME="flatpack2"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
PYTHON="${PYTHON:-python3}"

# Default config file to deploy (override with --config)
DEPLOY_CONFIG="flatpack2.conf"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --config)
            DEPLOY_CONFIG="$2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1"
            echo "Usage: $0 [--config <config_file>]"
            exit 1
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info()    { echo "[install] $*"; }
success() { echo "[install] ✓ $*"; }
warn()    { echo "[install] WARNING: $*"; }

require_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "ERROR: This script must be run as root (use sudo)."
        exit 1
    fi
}

require_file() {
    if [[ ! -f "$1" ]]; then
        echo "ERROR: Required file not found: $1"
        echo "Run this script from the flatpack2 source directory."
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
require_root

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

info "flatpack2 installer starting..."
info "Source dir : ${SCRIPT_DIR}"
info "Install dir: ${INSTALL_DIR}"
info "Config dir : ${CONFIG_DIR}"
info "Config file: ${DEPLOY_CONFIG}"

# Check required source files
require_file "flatpack2.py"
require_file "${DEPLOY_CONFIG}"
require_file "flatpack2.service"
require_file "requirements.txt"

# ---------------------------------------------------------------------------
# Stop service if running (upgrade scenario)
# ---------------------------------------------------------------------------
UPGRADE=false
if systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
    info "Stopping existing ${SERVICE_NAME} service..."
    systemctl stop "${SERVICE_NAME}"
    UPGRADE=true
fi

# ---------------------------------------------------------------------------
# Create directories
# ---------------------------------------------------------------------------
info "Creating directories..."
mkdir -p "${INSTALL_DIR}"
mkdir -p "${CONFIG_DIR}"

# ---------------------------------------------------------------------------
# Python venv
# ---------------------------------------------------------------------------
if [[ ! -d "${VENV_DIR}" ]]; then
    info "Creating Python virtual environment..."
    "${PYTHON}" -m venv "${VENV_DIR}"
    success "venv created at ${VENV_DIR}"
else
    info "Existing venv found at ${VENV_DIR} - reusing"
fi

info "Installing/upgrading Python dependencies..."
"${VENV_DIR}/bin/pip" install --upgrade pip --quiet
"${VENV_DIR}/bin/pip" install --upgrade -r requirements.txt --quiet
success "Dependencies installed"

# ---------------------------------------------------------------------------
# Copy program files
# ---------------------------------------------------------------------------
info "Installing program files..."
cp flatpack2.py "${INSTALL_DIR}/flatpack2.py"
cp requirements.txt "${INSTALL_DIR}/requirements.txt"
success "Program files copied to ${INSTALL_DIR}"

# ---------------------------------------------------------------------------
# Web-GUI JS assets (Chart.js) - vendored for fully offline operation.
# Non-fatal when offline: flatpack2 retries the download automatically on
# every start, and the dashboard falls back to the CDN while online.
# ---------------------------------------------------------------------------
info "Downloading Web-GUI JS assets (for offline dashboard graph)..."
STATIC_DIR="${INSTALL_DIR}/static"
mkdir -p "${STATIC_DIR}"
ASSET_URLS=(
    "https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"
    "https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"
)
ASSETS_OK=true
for url in "${ASSET_URLS[@]}"; do
    fname="$(basename "${url}")"
    if [[ -s "${STATIC_DIR}/${fname}" ]]; then
        info "  ${fname} already present - keeping"
        continue
    fi
    if curl -fsSL --connect-timeout 15 -o "${STATIC_DIR}/${fname}.part" "${url}" \
       && [[ "$(stat -c%s "${STATIC_DIR}/${fname}.part" 2>/dev/null || echo 0)" -gt 10240 ]]; then
        mv "${STATIC_DIR}/${fname}.part" "${STATIC_DIR}/${fname}"
        success "  ${fname} downloaded"
    else
        rm -f "${STATIC_DIR}/${fname}.part"
        ASSETS_OK=false
        warn "  ${fname} download FAILED (offline?)"
    fi
done
if [[ "${ASSETS_OK}" != "true" ]]; then
    warn "Web-GUI assets incomplete - dashboard graph needs internet until"
    warn "flatpack2 manages to download them on a later (online) start."
fi

# ---------------------------------------------------------------------------
# Config files - only copy if not already present (preserve user edits)
# ---------------------------------------------------------------------------
info "Checking config files..."
TARGET_CONF="${CONFIG_DIR}/flatpack2.conf"

if [[ ! -f "${TARGET_CONF}" ]]; then
    cp "${DEPLOY_CONFIG}" "${TARGET_CONF}"
    success "Config installed to ${TARGET_CONF}"
else
    warn "Config already exists at ${TARGET_CONF} - NOT overwritten (preserving your settings)"
    info "  To reset config: cp ${SCRIPT_DIR}/${DEPLOY_CONFIG} ${TARGET_CONF}"
fi

# Always copy the charger config template as reference (with .example suffix if exists)
if [[ -f "flatpack2_charger.conf" && ! -f "${CONFIG_DIR}/flatpack2_charger.conf" ]]; then
    cp flatpack2_charger.conf "${CONFIG_DIR}/flatpack2_charger.conf"
    success "Charger config template installed to ${CONFIG_DIR}/flatpack2_charger.conf"
fi

# ---------------------------------------------------------------------------
# Wrapper script
# ---------------------------------------------------------------------------
info "Creating wrapper script at ${BIN_LINK}..."
cat > "${BIN_LINK}" << WRAPPER
#!/usr/bin/env bash
# flatpack2 wrapper - activates venv and runs program
exec "${VENV_DIR}/bin/python" "${INSTALL_DIR}/flatpack2.py" "\$@"
WRAPPER
chmod +x "${BIN_LINK}"
success "Wrapper created: ${BIN_LINK}"

# ---------------------------------------------------------------------------
# systemd service
# ---------------------------------------------------------------------------
info "Installing systemd service..."

# Generate service file with correct paths and config
cat > "${SERVICE_FILE}" << SERVICE
[Unit]
Description=flatpack2 - Eltek Flatpack2 CAN controller
After=network.target

[Service]
Type=simple
ExecStart=${VENV_DIR}/bin/python ${INSTALL_DIR}/flatpack2.py --config ${TARGET_CONF}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=flatpack2

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
success "systemd service installed: ${SERVICE_FILE}"

# ---------------------------------------------------------------------------
# dialout group
# ---------------------------------------------------------------------------
REAL_USER="${SUDO_USER:-$USER}"
if [[ -n "${REAL_USER}" && "${REAL_USER}" != "root" ]]; then
    if ! groups "${REAL_USER}" | grep -q dialout; then
        info "Adding ${REAL_USER} to dialout group..."
        usermod -aG dialout "${REAL_USER}"
        success "${REAL_USER} added to dialout group (re-login required to take effect)"
    else
        success "${REAL_USER} is already in dialout group"
    fi
fi

# ---------------------------------------------------------------------------
# Enable and start service
# ---------------------------------------------------------------------------
systemctl enable "${SERVICE_NAME}"

if [[ "${UPGRADE}" == true ]]; then
    info "Restarting service after upgrade..."
    systemctl start "${SERVICE_NAME}"
    success "Service restarted"
else
    info "Starting service..."
    systemctl start "${SERVICE_NAME}"
    success "Service started"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "========================================"
echo "  flatpack2 installation complete"
echo "========================================"
echo "  Program  : ${INSTALL_DIR}/flatpack2.py"
echo "  Config   : ${TARGET_CONF}"
echo "  Command  : flatpack2 --config ${TARGET_CONF}"
echo "  Service  : systemctl status ${SERVICE_NAME}"
echo "  PTY      : screen /tmp/flatpack2.pty"
echo "  Web GUI  : http://localhost:8080"
echo "========================================"
if [[ "${UPGRADE}" == true ]]; then
    echo "  Upgraded from previous installation."
else
    echo "  Fresh installation complete."
    if ! groups "${REAL_USER:-root}" | grep -q dialout 2>/dev/null; then
        echo ""
        echo "  IMPORTANT: Log out and back in for dialout group to take effect."
    fi
fi
echo "========================================"
