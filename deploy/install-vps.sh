#!/usr/bin/env bash
# AIMurahV3 — bare-metal VPS installer (Ubuntu 22.04+ / Debian 12+).
# Run as root on a fresh VPS:  sudo bash deploy/install-vps.sh
#
# What it does:
#   1. Creates system user `aimurah` and /opt/aimurahv3 + /var/lib/aimurahv3.
#   2. Installs Python 3.11 venv and requirements.
#   3. Copies the checkout into /opt/aimurahv3 (if run from a clone, uses PWD).
#   4. Installs the systemd unit and a template env file.
#   5. Enables + starts the service.
#
# After it finishes:
#   - Edit /etc/aimurahv3/aimurahv3.env (adjust ports / secrets).
#   - Set the dashboard password:
#       sudo -u aimurah /opt/aimurahv3/.venv/bin/python -m aimurah set-password
#   - Put nginx/caddy in front for TLS (see deploy/nginx/aimurahv3.conf).

set -euo pipefail

APP_USER=aimurah
APP_DIR=/opt/aimurahv3
DATA_DIR=/var/lib/aimurahv3
ETC_DIR=/etc/aimurahv3
SERVICE_NAME=aimurahv3.service
SOURCE_DIR="${SOURCE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

require_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "error: run as root (use sudo)" >&2
        exit 1
    fi
}

install_apt_deps() {
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip \
        ca-certificates curl rsync
}

ensure_user() {
    if ! id -u "$APP_USER" >/dev/null 2>&1; then
        useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$APP_USER"
    fi
}

sync_source() {
    mkdir -p "$APP_DIR"
    rsync -a --delete \
        --exclude ".git" \
        --exclude ".venv" \
        --exclude "tests" \
        --exclude "__pycache__" \
        --exclude ".aimurahv3" \
        "$SOURCE_DIR"/ "$APP_DIR"/
    chown -R "$APP_USER:$APP_USER" "$APP_DIR"
}

install_venv() {
    if [[ ! -d "$APP_DIR/.venv" ]]; then
        python3 -m venv "$APP_DIR/.venv"
    fi
    "$APP_DIR/.venv/bin/pip" install --upgrade pip wheel
    "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
    chown -R "$APP_USER:$APP_USER" "$APP_DIR/.venv"
}

ensure_data_dir() {
    mkdir -p "$DATA_DIR"
    chown -R "$APP_USER:$APP_USER" "$DATA_DIR"
    chmod 750 "$DATA_DIR"
}

install_env_file() {
    mkdir -p "$ETC_DIR"
    chmod 750 "$ETC_DIR"
    if [[ ! -f "$ETC_DIR/aimurahv3.env" ]]; then
        cp "$SOURCE_DIR/.env.example" "$ETC_DIR/aimurahv3.env"
        sed -i "s|^AIMURAH_HOME=.*|AIMURAH_HOME=$DATA_DIR|" "$ETC_DIR/aimurahv3.env"
        chmod 640 "$ETC_DIR/aimurahv3.env"
        chown root:"$APP_USER" "$ETC_DIR/aimurahv3.env"
        echo "wrote $ETC_DIR/aimurahv3.env (edit before restart)"
    fi
}

install_systemd_unit() {
    cp "$SOURCE_DIR/deploy/systemd/$SERVICE_NAME" "/etc/systemd/system/$SERVICE_NAME"
    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
}

main() {
    require_root
    install_apt_deps
    ensure_user
    sync_source
    install_venv
    ensure_data_dir
    install_env_file
    install_systemd_unit
    systemctl restart "$SERVICE_NAME"
    sleep 2
    systemctl --no-pager status "$SERVICE_NAME" || true
    cat <<EOF

Done. Next steps:
  1. Edit /etc/aimurahv3/aimurahv3.env (network bind, secrets).
  2. Set the dashboard password:
       sudo -u $APP_USER $APP_DIR/.venv/bin/python -m aimurah set-password
  3. (Optional) Copy deploy/nginx/aimurahv3.conf into nginx and issue TLS certs.
  4. Check logs:  journalctl -u $SERVICE_NAME -f
EOF
}

main "$@"
