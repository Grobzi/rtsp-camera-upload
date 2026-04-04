#!/usr/bin/env bash
# Install and enable a systemd service for the camera uploader.
# Usage:
#   sudo ./scripts/install_systemd_service.sh --user pi --service-name camera-uploader --python /usr/bin/python3 --config /home/pi/mfsg-camera/config/cameras.json --env /home/pi/mfsg-camera/config/.env

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${SCRIPT_DIR}/.."

SERVICE_NAME="camera-uploader"
SERVICE_USER="$(whoami)"
PYTHON="$(command -v python3 || echo /usr/bin/python3)"
CONFIG_PATH="${REPO_ROOT}/config/cameras.json"
ENV_PATH="${REPO_ROOT}/config/.env"
WORKING_DIR="${REPO_ROOT}"
DESCRIPTION="Multi-camera snapshot uploader"

usage(){
  echo "Usage: $0 [--service-name NAME] [--user USER] [--python PATH] [--config PATH] [--env PATH] [--working-dir PATH]" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --service-name) SERVICE_NAME="$2"; shift 2;;
    --user) SERVICE_USER="$2"; shift 2;;
    --python) PYTHON="$2"; shift 2;;
    --config) CONFIG_PATH="$2"; shift 2;;
    --env) ENV_PATH="$2"; shift 2;;
    --working-dir) WORKING_DIR="$2"; shift 2;;
    -h|--help) usage;;
    *) echo "Unknown arg: $1"; usage;;
  esac
done

if [[ $(id -u) -ne 0 ]]; then
  echo "This script must be run with sudo or as root." >&2
  exit 2
fi

if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "Config file not found: $CONFIG_PATH" >&2
  exit 2
fi

if [[ ! -f "$ENV_PATH" ]]; then
  echo "Env file not found: $ENV_PATH" >&2
  exit 2
fi

UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

echo "Creating systemd unit at $UNIT_PATH"

cat > "$UNIT_PATH" <<EOF
[Unit]
Description=${DESCRIPTION}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${WORKING_DIR}
EnvironmentFile=${ENV_PATH}
ExecStart=${PYTHON} ${REPO_ROOT}/scripts/upload_snapshot.py --config ${CONFIG_PATH} --env ${ENV_PATH}
Restart=always
RestartSec=10
StartLimitIntervalSec=0

[Install]
WantedBy=multi-user.target
EOF

chmod 644 "$UNIT_PATH"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"
systemctl restart "${SERVICE_NAME}.service"

echo "Service ${SERVICE_NAME} installed and started."
echo "Check status with: systemctl status ${SERVICE_NAME}.service"
