#!/usr/bin/env bash
# flatpack2 uninstall script
# Copyright (c) 2026 mti@mti.sk | Coded by Claude Sonnet 4.6
# https://github.com/mti-sk/flatpack2
#
# Usage:
#   sudo bash uninstall.sh              # removes everything except config
#   sudo bash uninstall.sh --purge      # removes everything including config

set -euo pipefail

INSTALL_DIR="/opt/flatpack2"
CONFIG_DIR="/etc/flatpack2"
BIN_LINK="/usr/local/bin/flatpack2"
SERVICE_NAME="flatpack2"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

PURGE=false

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --purge)
            PURGE=true
            shift
            ;;
        *)
            echo "Unknown argument: $1"
            echo "Usage: $0 [--purge]"
            exit 1
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info()    { echo "[uninstall] $*"; }
success() { echo "[uninstall] ✓ $*"; }
warn()    { echo "[uninstall] WARNING: $*"; }

require_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "ERROR: This script must be run as root (use sudo)."
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
require_root

echo "========================================"
echo "  flatpack2 uninstaller"
echo "========================================"
if [[ "${PURGE}" == true ]]; then
    echo "  Mode: PURGE (config will also be removed)"
else
    echo "  Mode: standard (config preserved)"
    echo "  Use --purge to also remove config files"
fi
echo "========================================"
echo ""

read -r -p "Continue with uninstall? [y/N] " confirm
if [[ "${confirm}" != "y" && "${confirm}" != "Y" ]]; then
    echo "Aborted."
    exit 0
fi

# ---------------------------------------------------------------------------
# Stop and disable service
# ---------------------------------------------------------------------------
if systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
    info "Stopping ${SERVICE_NAME} service..."
    systemctl stop "${SERVICE_NAME}"
    success "Service stopped"
fi

if systemctl is-enabled --quiet "${SERVICE_NAME}" 2>/dev/null; then
    info "Disabling ${SERVICE_NAME} service..."
    systemctl disable "${SERVICE_NAME}"
    success "Service disabled"
fi

# ---------------------------------------------------------------------------
# Remove service file
# ---------------------------------------------------------------------------
if [[ -f "${SERVICE_FILE}" ]]; then
    rm -f "${SERVICE_FILE}"
    systemctl daemon-reload
    success "Service file removed: ${SERVICE_FILE}"
fi

# ---------------------------------------------------------------------------
# Remove wrapper
# ---------------------------------------------------------------------------
if [[ -L "${BIN_LINK}" || -f "${BIN_LINK}" ]]; then
    rm -f "${BIN_LINK}"
    success "Wrapper removed: ${BIN_LINK}"
fi

# ---------------------------------------------------------------------------
# Remove PTY symlink if present
# ---------------------------------------------------------------------------
if [[ -L "/tmp/flatpack2.pty" ]]; then
    rm -f "/tmp/flatpack2.pty"
    success "PTY symlink removed"
fi

# ---------------------------------------------------------------------------
# Remove install directory
# ---------------------------------------------------------------------------
if [[ -d "${INSTALL_DIR}" ]]; then
    rm -rf "${INSTALL_DIR}"
    success "Install directory removed: ${INSTALL_DIR}"
fi

# ---------------------------------------------------------------------------
# Config - only remove with --purge
# ---------------------------------------------------------------------------
if [[ "${PURGE}" == true ]]; then
    if [[ -d "${CONFIG_DIR}" ]]; then
        rm -rf "${CONFIG_DIR}"
        success "Config directory removed: ${CONFIG_DIR}"
    fi
else
    if [[ -d "${CONFIG_DIR}" ]]; then
        warn "Config preserved at ${CONFIG_DIR}"
        info "  Run with --purge to also remove config"
    fi
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "========================================"
echo "  flatpack2 uninstall complete"
echo "========================================"
if [[ "${PURGE}" == false && -d "${CONFIG_DIR}" ]]; then
    echo "  Config files preserved at: ${CONFIG_DIR}"
    echo "  Remove manually or run: sudo bash uninstall.sh --purge"
fi
echo "========================================"
