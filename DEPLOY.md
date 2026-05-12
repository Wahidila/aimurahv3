# AIMurahV3 VPS Deployment Guide

Three supported ways to run AIMurahV3 on a Linux VPS, ordered by
complexity:

1. **systemd + venv** (recommended for bare-metal Ubuntu/Debian)
2. **Docker Compose** (if you already run containers on the host)
3. **Manual foreground run** (useful for quick testing only)

All three expect a fresh VPS with SSH root or sudo access, Python 3.11+,
and port 80/443 reachable.

> Data directory (`AIMURAH_HOME`) holds the SQLite DB, logs, PID file,
> and config. Defaults to `~/.aimurahv3`; the systemd unit uses
> `/var/lib/aimurahv3`. Back this directory up — it is the source of
> truth for accounts and API keys.

---

## 1. systemd + venv (recommended)

One-shot installer:

```bash
git clone <your-repo-url> /tmp/aimurahv3
cd /tmp/aimurahv3
sudo bash deploy/install-vps.sh
```

The script:

- creates the `aimurah` system user,
- syncs the repo into `/opt/aimurahv3`,
- builds a venv at `/opt/aimurahv3/.venv`,
- writes `/etc/aimurahv3/aimurahv3.env` from `.env.example`,
- installs and enables `aimurahv3.service`.

After it finishes:

```bash
sudo nano /etc/aimurahv3/aimurahv3.env     # adjust ports / secrets
sudo -u aimurah /opt/aimurahv3/.venv/bin/python -m aimurah set-password
sudo systemctl restart aimurahv3
sudo journalctl -u aimurahv3 -f
```

Proxy: `http://127.0.0.1:7830` · Dashboard: `http://127.0.0.1:7831`.

### TLS with nginx

```bash
sudo apt install -y nginx
sudo cp /opt/aimurahv3/deploy/nginx/aimurahv3.conf /etc/nginx/sites-available/aimurahv3
# edit server_name fields, then:
sudo ln -s /etc/nginx/sites-available/aimurahv3 /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d proxy.example.com -d dash.example.com
```

Once TLS is live, flip `AIMURAH_COOKIE_SECURE=1` in
`/etc/aimurahv3/aimurahv3.env` and restart.

---

## 2. Docker Compose

```bash
git clone <your-repo-url> /opt/aimurahv3
cd /opt/aimurahv3
cp .env.example .env            # edit as needed
docker compose build
docker compose up -d
docker compose logs -f
```

Data persists in the named volume `aimurahv3-data`. Set the dashboard
password:

```bash
docker compose exec aimurahv3 python -m aimurah set-password
```

The published ports are bound to `127.0.0.1` only. Front them with
nginx or caddy on the host for TLS — same `deploy/nginx/aimurahv3.conf`
works.

---

## 3. Manual foreground run

Only for short-lived testing:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m aimurah start --foreground
```

Stop with Ctrl+C. Data goes to `~/.aimurahv3/`.

---

## Configuration precedence

```
built-in DEFAULTS  <  config.json  <  environment variables
```

Environment variables override config.json when set non-empty:

| Variable | Notes |
|---|---|
| `AIMURAH_HOME` | Data directory (SQLite, logs, pid). |
| `AIMURAH_PROXY_HOST` / `AIMURAH_PROXY_PORT` | Proxy bind. |
| `AIMURAH_DASHBOARD_HOST` / `AIMURAH_DASHBOARD_PORT` | Dashboard bind. |
| `AIMURAH_API_KEY` | Static proxy API key. Leave empty to auto-generate. |
| `AIMURAH_SESSION_SECRET` | Cookie signing secret. |
| `AIMURAH_COOKIE_SECURE` | `1` when behind HTTPS. |
| `AIMURAH_UPSTREAM_PROXY` | Outbound proxy for Kiro traffic. |
| `AIMURAH_LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR`. |
| `AIMURAH_LOG_MAX_BYTES`, `AIMURAH_LOG_BACKUP_COUNT` | Log rotation. |
| `AIMURAH_REQUEST_LOG_MAX_BYTES` | JSONL rotation threshold. |

---

## Security checklist

- [x] Proxy API auth uses constant-time compare.
- [x] Dashboard login has in-memory + nginx rate limits.
- [x] Session cookies are HttpOnly, SameSite=Lax, and `Secure` when
      `AIMURAH_COOKIE_SECURE=1`.
- [x] Log files are rotated (bounded disk use).
- [x] No payload dumps to disk in normal operation.
- [ ] Put the proxy behind TLS before opening it to the public.
- [ ] Back up `AIMURAH_HOME` regularly (contains tokens + API keys).

---

## Operational tips

- **First boot:** the app writes `config.json` with auto-generated
  `api_key` and `dashboard_session_secret`. Rotate either via
  `python -m aimurah apikey --rotate` or the dashboard.
- **Resetting a lockout:** restart the service (`systemctl restart
  aimurahv3` or `docker compose restart`). The login-failure counter is
  in-memory.
- **Getting the API key programmatically:**
  `python -m aimurah apikey`.
- **Upgrading:** `git pull && bash deploy/install-vps.sh` — the script
  is idempotent. State in `AIMURAH_HOME` survives.
