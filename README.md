# AIMurahV3

Self-hosted proxy gateway that pools multiple Kiro accounts (Free and Pro) and
exposes them as OpenAI- and Anthropic-compatible endpoints.

**Default ports:**
- Proxy API: `http://127.0.0.1:7830`
- Dashboard: `http://127.0.0.1:7831`

> Running on a VPS? See [`DEPLOY.md`](./DEPLOY.md) for systemd / Docker
> Compose installers, nginx TLS config, and the full environment variable
> reference.

## Highlights

- **IDE-impersonation headers** — the proxy sends exactly what Kiro IDE sends
  (`User-Agent: AWS-SDK-JS/3.0.0 kiro-ide/1.0.0`,
  `X-Amz-User-Agent: aws-sdk-js/3.0.0 kiro-ide/1.0.0`,
  `X-Amz-Target: AmazonCodeWhispererStreamingService.GenerateAssistantResponse`,
  `Accept: application/vnd.amazon.eventstream`). Upstream treats our traffic
  as legitimate editor sessions rather than third-party bots.
- **IDE-shaped request body** — `conversationState` with `chatTriggerType:"MANUAL"`,
  `modelId` + `origin:"AI_EDITOR"` inside each `userInputMessage`, a
  `[Context: Current time is …]` prefix on the current turn, and
  `inferenceConfig` at the root.
- **Amazon eventstream parser** — handles both
  `application/vnd.amazon.eventstream` binary frames (used by the streaming
  service) and plain `data: {...}\n\n` SSE fallback.
- **Import-refresh-token onboarding (recommended)** — paste the refresh token
  from your real Kiro IDE session. Upstream continues to recognize those
  tokens as legitimate editor credentials, which is why this flow is far
  more rate-limit-resistant than a fresh browser OAuth.
- **OAuth Kiro (PKCE + S256)** is still supported as a fallback for Google /
  GitHub social sign-in via the Kiro desktop login URL.
- **Token manager** — auto-refresh at `prod.us-east-1.auth.desktop.kiro.dev/refreshToken`
  before expiry, per-account locking, retry on 401/403. IDC/Builder-ID device
  flow uses `oidc.us-east-1.amazonaws.com/token` with stored `clientId` /
  `clientSecret`.
- **Plan detection** — polls `q.us-east-1.amazonaws.com/getUsageLimits`
  (with `profileArn`), parses `subscriptionInfo` / `subscriptionTitle` /
  `subscriptionType` / `tier` to detect `free` vs `pro`. Case-insensitive
  word match, so `Kiro Pro`, `Team Plan`, `Business Plus` all normalize to `pro`.
- **Model catalog** — static baseline + live refresh via
  `AmazonCodeWhispererService.ListAvailableModels`. Models returned only by
  Pro accounts are flagged `requires_pro`.
- **Account pool with retry-on-429** — round-robin with `sticky_round_robin_limit`
  (default 3), per-model cooldown locks (`modelLock_<id>`), exponential
  backoff (`base=2s * 2^(level-1)`, capped at 300s, max level 15). Incoming
  requests automatically rotate to another account on 429/5xx without
  bubbling the error to the client.
- **Dashboard** — password-protected SPA with account management, model
  catalog refresh, usage charts, API key rotation, settings, and a first-run
  setup screen.

## Install

```powershell
cd D:\AIMurahV3
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Optional (automated OAuth):
pip install camoufox browserforge playwright
python -m playwright install firefox
python -m camoufox fetch
```

## CLI

AIMurahV3 ships with a daemon-style CLI. On Windows use `aimurahv3.cmd` or
`aimurahv3.ps1`; on macOS/Linux use `./aimurahv3`.

```powershell
# Windows PowerShell
.\aimurahv3.ps1 start       # start in background
.\aimurahv3.ps1 status      # show status
.\aimurahv3.ps1 logs -f     # follow logs
.\aimurahv3.ps1 stop        # stop daemon
.\aimurahv3.ps1 restart
.\aimurahv3.ps1 apikey --rotate
.\aimurahv3.ps1 set-password
```

```cmd
REM Windows cmd.exe
aimurahv3.cmd start
aimurahv3.cmd status
aimurahv3.cmd stop
```

```bash
# macOS / Linux (after `chmod +x aimurahv3`)
./aimurahv3 start
./aimurahv3 status
./aimurahv3 logs -f
./aimurahv3 stop
```

All forms are equivalent to `python -m aimurah <command>`.

### Subcommands

| Command | Description |
|---|---|
| `start [--foreground] [--host H] [--proxy-port N] [--dashboard-port N]` | Start the daemon |
| `stop` | Stop the running daemon |
| `restart` | Restart the daemon |
| `status` | Show daemon status, PID, ports |
| `version` | Print version |
| `logs [-n N] [-f]` | Show or follow the log file |
| `set-password [--password PASS]` | Set the dashboard password |
| `apikey [--rotate]` | Print or rotate the proxy API key |

`start --foreground` runs attached to your terminal (useful for `Ctrl+C`).
`start` without `--foreground` detaches and writes a PID file into
`%USERPROFILE%\.aimurahv3\aimurahv3.pid`.

### Changing ports

You can either pass `--proxy-port` / `--dashboard-port` to `start`, or edit
`%USERPROFILE%\.aimurahv3\config.json` and run `aimurahv3 restart`.

## Using the proxy

```bash
# OpenAI style
curl -H "Authorization: Bearer <YOUR_AIMURAH_API_KEY>" \
     -H "Content-Type: application/json" \
     -d '{"model":"claude-opus-4.7","stream":true,
          "messages":[{"role":"user","content":"Halo"}]}' \
     http://127.0.0.1:7830/v1/chat/completions

# Anthropic style
curl -H "x-api-key: <YOUR_AIMURAH_API_KEY>" \
     -H "Content-Type: application/json" \
     -d '{"model":"claude-opus-4.7","max_tokens":1024,
          "messages":[{"role":"user","content":"Halo"}]}' \
     http://127.0.0.1:7830/v1/messages
```

The API key is generated on first run and shown on the dashboard overview.
Rotate it from the dashboard or via `aimurahv3 apikey --rotate`.

## First-time setup

1. `./aimurahv3 start`
2. Open `http://127.0.0.1:7831/` and set a dashboard password (or use
   `./aimurahv3 set-password` first).
3. Click **Add Kiro account** and pick the **Import refresh token (recommended)**
   tab. Paste your Kiro IDE refresh token (starts with `aorAAAAAG`) and hit
   **Add account**. The proxy validates the token against
   `prod.us-east-1.auth.desktop.kiro.dev/refreshToken`, stores the result,
   and immediately syncs usage / plan type.
4. Repeat for additional accounts. Pro accounts unlock `claude-opus-4.7`,
   `claude-opus-4.6`, `claude-sonnet-4.6`, `claude-opus-4.5`.
5. Browser OAuth is still available under the **OAuth flow** tab as a
   fallback, but import-token is far more stable against rate limits.

### Where to find a Kiro refresh token

The refresh token lives inside your Kiro IDE's auth state. Easiest ways to
extract it:

- Kiro IDE → DevTools → Network → filter `refreshToken` → inspect the
  request body.
- `~/.aws/sso/cache/*.json` after signing into Kiro (fields: `refreshToken`,
  `accessToken`, `profileArn`).
- On Windows: `%USERPROFILE%\AppData\Roaming\Kiro\User\globalStorage\*.json`
  (or `%LOCALAPPDATA%\Kiro\...`), depending on the build.

The token is a long opaque string starting with `aorAAAAAG`. Don't share
it — it grants full access to that Kiro account.

## Project layout

```
D:\AIMurahV3\
├── aimurahv3.cmd                  # Windows cmd launcher
├── aimurahv3.ps1                  # Windows PowerShell launcher
├── aimurahv3                      # POSIX launcher
├── run.py                         # Compatibility launcher (= start --foreground)
├── requirements.txt
└── aimurah\
    ├── __main__.py                # python -m aimurah ...
    ├── cli.py                     # CLI subcommands (start/stop/status/etc.)
    ├── daemon.py                  # Foreground daemon runner
    ├── config.py                  # On-disk config + password hashing
    ├── logs.py                    # Structured logs
    ├── storage.py                 # SQLite DAL
    ├── kiro\
    │   ├── common.py              # Endpoints, PKCE, plan parsing, static model list
    │   ├── oauth.py               # Login URL, manual + Camoufox flows
    │   ├── auth.py                # Token refresh manager
    │   ├── usage.py               # Usage/plan_type sync loop
    │   ├── catalog.py             # Model catalog (static + live refresh)
    │   ├── pool.py                # Account selector
    │   └── client.py              # Kiro request builder, SSE parser, formatters
    ├── proxy\
    │   └── server.py              # /v1/chat/completions, /v1/messages, /v1/models
    └── dashboard\
        ├── server.py              # REST API + auth
        └── static\                # HTML / CSS / JS
```

## Data directory

All runtime state (config, SQLite DB, logs, PID file) lives under
`%USERPROFILE%\.aimurahv3\`. Set `AIMURAH_HOME` to override.

```
.aimurahv3\
├── config.json
├── store.db
├── aimurahv3.log
├── aimurahv3.pid
└── request_logs.jsonl
```

## Notes

- **Why the import-token flow avoids rate limits.** The upstream applies
  softer limits to traffic that carries a valid editor session token. Browser
  OAuth sessions (fresh tokens created outside the IDE) plus generic
  `User-Agent` strings trip heuristics that mark the account as a bot. Sending
  the Kiro IDE's exact headers and a refresh token minted by the real IDE
  keeps us under the "legitimate editor" bucket. The proxy also speaks the
  service's expected body shape (`origin:"AI_EDITOR"`, `modelId` inside
  `userInputMessage`, context prefix, `inferenceConfig`), which the backend
  uses to cluster requests.
- **Retry-on-429.** When an account gets a 429, the proxy records it (per
  model!), picks another eligible account from the pool, and retries up to
  two times before surfacing an error to the client. If all accounts are
  locked, a 503 with `Retry-After` is returned.
- **Plan detection** accepts `pro`, `plus`, `team`, `business`, or
  `enterprise` as Pro (case-insensitive, whole-word match against the
  subscription title field).
- The SSE parser (`kiro/client.py::EventStreamParser`) handles both binary
  eventstream frames and plain SSE fallback.
- Nothing in this repository ships Kiro credentials, the Kiro binary, or any
  AWS SDK code. You authenticate with your own Kiro account.
