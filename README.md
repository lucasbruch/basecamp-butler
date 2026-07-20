# 🎬 Basecamp Butler

A self-hosted, zero-cost personal assistant for a **regular** Basecamp member.
It polls your Basecamp account every few minutes, tracks what's new across your
projects, and acts as a lightweight coordinator: surfacing new activity,
suggesting to-dos, letting you confirm/dismiss them from a web UI or straight
from a phone push notification, and reminding you before things are due.

**Basic out of the box** — the default classifier reads generic work vocabulary
(documents, deliverables, tickets, meetings, deadlines…), so it's useful for
anyone. Anything more specific is up to you: switch on the LLM classifier and
define a **persona** (character + topics) that fits your own work, from the
Settings page.

- **No admin rights needed.** Uses OAuth as a normal member — polling only, no
  webhooks/SCIM. The API inherits whatever *you* can already see.
- **No paid anything.** Local Postgres, free push via ntfy (or a Telegram bot),
  optional local Ollama LLM. No SaaS.

> **New here?** Follow the step-by-step [Setup Guide (0 to 100)](SETUP.md), a
> no-experience-needed walkthrough from a blank NAS to remote access over a
> corporate VPN. The rest of this README is the condensed technical reference.

## Architecture

```
Basecamp REST ──poll every ~5 min──▶ poller ──▶ Postgres ──▶ ┌ Web UI (FastAPI/:8000)
                                     (Python)   events/todos  ├ Notifier (ntfy | Telegram)
                                                              └ Classifier (rules | Ollama)
```

Everything runs from one `docker-compose.yml`: `db` (Postgres) + `app` (poller +
scheduler + web UI + notifier in a single process). Ollama is optional and
off by default.

## Quick start

1. **Register a Basecamp integration** (as yourself, no admin needed) at
   <https://launchpad.37signals.com/integrations>. Set the redirect URI to
   `http://localhost:8000/oauth/callback`. Note the Client ID/Secret.

2. **Set up notifications** (default is **ntfy** — no account, no bot):
   install the [ntfy app](https://ntfy.sh/), subscribe to a hard-to-guess topic
   like `basecamp-butler-a8f3k2x9`, and put that topic in `NTFY_TOPIC`.
   *(Prefer Telegram? Set `NOTIFY_CHANNEL=telegram` and provide a @BotFather
   token + your chat id instead.)*

3. **Configure**:
   ```bash
   cp .env.example .env
   # fill in BASECAMP_CLIENT_ID/SECRET and NTFY_TOPIC (or the Telegram vars)
   ```

4. **Authorize Basecamp once** (interactive OAuth handshake, stores tokens in DB):
   ```bash
   docker compose run --rm --service-ports app python scripts/authorize.py
   ```
   Open the printed URL, approve, done.

5. **Run**:
   ```bash
   docker compose up -d
   ```
   Open <http://localhost:8000>.

## Deploy to a Synology NAS via Portainer (Git stack)

This is the low-maintenance path: Portainer clones this repo, builds the image
itself, and re-deploys when you push updates — no manual file transfer, no SSH.

1. **Stacks → Add stack → Repository.**
   - **Repository URL**: `https://github.com/lucasbruch/basecamp-butler`
   - **Reference**: `refs/heads/main`
   - **Compose path**: `docker-compose.yml`
   - Leave **Authentication** off — the repo is public. (If you fork it private,
     toggle Authentication on and supply your GitHub username + a fine-grained
     PAT with Contents: Read.)

2. **Environment variables** (in the stack's env panel — the compose reads these
   via `${VAR}` substitution):
   ```
   POSTGRES_USER=basecamp
   POSTGRES_PASSWORD=pick-a-strong-password
   POSTGRES_DB=basecamp
   BASECAMP_CLIENT_ID=your-client-id
   BASECAMP_CLIENT_SECRET=your-client-secret
   BASECAMP_REDIRECT_URI=http://<NAS-IP>:8000/oauth/callback
   BASECAMP_USER_AGENT=BasecampButtler (you@example.com)
   NOTIFY_CHANNEL=ntfy
   NTFY_TOPIC=basecamp-butler-change-me-to-something-random
   APP_BASE_URL=http://<NAS-IP>:8000
   WEB_PORT=8000
   ```
   (`NTFY_SERVER`, `POLL_INTERVAL_MINUTES`, `DUE_SOON_DAYS`, `CLASSIFIER` have safe
   defaults. `APP_BASE_URL` makes the notification buttons work — set it to the
   same address you use to reach the app.)

3. Enable **Automatic updates** (poll the repo, e.g. every 5 min) or set up the
   redeploy **webhook** so a `git push` rolls out on its own. **Deploy the stack.**

4. In your [Basecamp integration](https://launchpad.37signals.com/integrations),
   set the **Redirect URI** to exactly `http://<NAS-IP>:8000/oauth/callback`.

5. Authorize from any browser on your LAN: open
   `http://<NAS-IP>:8000/settings` → **Connect Basecamp →** → approve.
   (No `authorize.py` needed — that's only for a desktop setup.)

**Updating later:** just `git push`. Portainer re-pulls and rebuilds (auto if you
enabled it, or **Pull and redeploy** in the stack). Your DB volume and stored
tokens persist across redeploys.

> No-git alternative: [`deploy/portainer-stack.yml`](deploy/portainer-stack.yml)
> references a pre-built `basecamp-butler:latest` image if you'd rather build it
> once over SSH than let Portainer build.

## Remote access over a corporate VPN

A full-tunnel, IT-managed VPN grabs the default route (and often overlaps home
`192.168.x.x` ranges) the moment it connects, so from that machine the NAS's LAN
IP `http://<NAS-IP>:8000` stops resolving. If Ollama runs on that same machine,
the NAS also loses it (`OLLAMA_URL` unreachable) and the LLM classifier silently
fails. You usually can't change an IT-locked VPN's routing, so the fix has to sit
*on top of* it.

**[Tailscale](https://tailscale.com)** (a WireGuard mesh) solves both at once:
each device gets a stable `100.x` tailnet IP reachable regardless of the VPN's
routing, end-to-end encrypted, with no port forwarding or public exposure. It
egresses over the VPN's own default route (relaying over HTTPS when direct UDP is
blocked), so it keeps working *while* the VPN is connected.

1. Install Tailscale on the **NAS** (Synology Package Center → Tailscale → sign
   in) and on the **machine that hosts Ollama / your browser**; join the same
   tailnet. No container change — the app already publishes `8000` on the host.
2. Browse to `http://<nas-tailnet-ip>:8000` (enable **MagicDNS** for a stable
   name like `http://nas:8000`) instead of the LAN IP when on the VPN.
3. For **NAS → Ollama**: run Ollama with `OLLAMA_HOST=0.0.0.0`, set
   `OLLAMA_URL=http://<ollama-tailnet-ip>:11434` in Portainer, and **Pull and
   redeploy**. Firewall port `11434` to the tailnet range `100.64.0.0/10` so only
   the tailnet can reach it.
4. Set **`WEB_AUTH_TOKEN`** (defense in depth) so the UI + API require the token
   even inside the tailnet; the ntfy buttons send it automatically.

> Ultra-strict clients (e.g. Zscaler / GlobalProtect "block outside access")
> firewall *all* non-VPN interfaces and can stop even Tailscale. Fallback: expose
> only the web UI via an outbound **Cloudflare Tunnel** (`cloudflared`) gated by
> Cloudflare Access, and move Ollama off the VPN'd machine (an always-on LAN box
> or the NAS itself).

## How it decides what's a to-do (v1, rules)

Deterministic heuristics in [`app/classifier/rules.py`](app/classifier/rules.py):

- A **to-do assigned to you** → suggested to-do (with reminder if it has a due date).
- A **to-do due soon and unassigned** (within `DUE_SOON_DAYS`) → suggested to-do.
- A **message / comment / chat line that names you** → suggested to-do.
- A message/comment/chat carrying an **action signal + a work noun** (e.g. "please
  send the budget", "can you review the deck before Friday") → suggested to-do.
- A **Ping (direct message)** that reads like an ask → suggested to-do, tagged with
  the sender. Pings are higher-signal (aimed at you), so either gate is enough.

### What it reads

To-dos, message-board posts, comments, **Campfire** chat lines, and **Pings**
(1:1 / small-group DMs). Pings aren't in the projects/recordings index — they're
pulled from the account notifications feed (`/my/readings.json`, `section: pings`)
and read via the same chat-lines endpoint as Campfire. Toggle the last two with
`POLL_CAMPFIRE` / `POLL_PINGS`. Everything respects your Basecamp visibility.

Every suggestion lands as `status = suggested` — never auto-confirmed — unless
you enable **auto-add** for that project on the Settings page, in which case it
lands as `confirmed`.

### Web UI

- **Dashboard** (`/`) — active to-dos with a health strip showing last-poll
  status so you can tell at a glance if polling is stuck.
- **To-dos** (`/todos`) — review, confirm, dismiss, or mark done.
- **Activity** (`/activity`) — a raw feed of everything ingested, whether or not
  it became a to-do.
- **Settings** (`/settings`) — connect Basecamp, per-project auto-add, and the
  editable assistant persona.

The dashboard, to-dos, and activity pages soft-refresh on their own, so they stay
current without a manual reload.

## Upgrading to the LLM classifier (v2, optional)

Set `CLASSIFIER=ollama` in `.env` and add an `ollama` service. See
[`app/classifier/ollama.py`](app/classifier/ollama.py).

The **assistant persona is fully editable** on the Settings page — no code change
needed. Out of the box it's a plain, general-purpose assistant; you define
anything more specific yourself by giving it a character (role) and the topics it
should watch for, or by overriding the whole system prompt, and you can run a
sample message through it live before saving. Overrides are stored in `app_state`,
so they take effect without a restart. Add to `docker-compose.yml`:

```yaml
  ollama:
    image: ollama/ollama:latest
    restart: unless-stopped
    volumes:
      - ollama:/root/.ollama
# and add `ollama:` under top-level volumes, then:
#   docker compose exec ollama ollama pull llama3.1:8b
```

## Notifications

Set the channel with `NOTIFY_CHANNEL` (`ntfy` | `telegram` | `none`). Each new
suggestion/reminder is pushed with **✅ Add / ✖ Dismiss / Open** action buttons
(a confirmed to-do shows **✔ Done** instead of Add).

- **ntfy (default):** push to `NTFY_SERVER/NTFY_TOPIC` — no account, no bot. The
  buttons POST back to this app's `/api/todos/{id}/{action}` routes, so set
  `APP_BASE_URL` to an address your phone can reach (same LAN, or via VPN /
  [Tailscale](https://tailscale.com) when away from home). Without `APP_BASE_URL`
  you still get notifications, just no buttons.
- **telegram:** inline buttons handled via bot long-polling (works anywhere, no
  public URL needed). Set `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`.

Both live behind `app/notifier/` — adding another channel is a small module.

## Data model

`projects`, `raw_events` (jsonb payloads), `todos`, `reminders`, `oauth_tokens`
(single row), plus `checkpoints` (per-type `updated_at` high-water mark) and
`app_state` (small kv). See [`app/models.py`](app/models.py). The schema is
managed by **Alembic** ([`migrations/`](migrations)) — the app runs
`alembic upgrade head` on boot, falling back to `create_all()` for a fresh
install if that fails.

## Env reference

| Var | Meaning |
|---|---|
| `BASECAMP_CLIENT_ID` / `_SECRET` | From your Launchpad integration |
| `BASECAMP_REDIRECT_URI` | Must match the integration; default `http://localhost:8000/oauth/callback` |
| `BASECAMP_USER_AGENT` | Basecamp requires a UA with contact info |
| `NOTIFY_CHANNEL` | `ntfy` (default), `telegram`, or `none` |
| `NTFY_SERVER` / `NTFY_TOPIC` | ntfy server (default `https://ntfy.sh`) + your topic |
| `NTFY_TOKEN` | Optional, for protected/self-hosted ntfy topics |
| `APP_BASE_URL` | This app's reachable URL — powers notification buttons |
| `TELEGRAM_BOT_TOKEN` / `_CHAT_ID` | Only if `NOTIFY_CHANNEL=telegram` |
| `POLL_INTERVAL_MINUTES` | Poll cadence (default 5) |
| `DUE_SOON_DAYS` | "Due soon" threshold (default 3) |
| `POLL_CAMPFIRE` / `POLL_PINGS` | Ingest Campfire chat / Pings (both default `true`) |
| `WEB_AUTH_TOKEN` | Optional secret to lock the UI + API behind HTTP Basic (blank = open, LAN-only) |
| `CLASSIFIER` | `rules` (default) or `ollama` |
| `OLLAMA_URL` / `OLLAMA_MODEL` | For the v2 classifier |

## Notes & limits

- Access tokens expire ~every 2 weeks; the poller auto-refreshes using the
  stored refresh token before each run.
- Rate limit is 50 req / 10 s per token — the client throttles and honours
  `Retry-After` on 429.
- First poll only **seeds** the checkpoints (no historical backfill) so you
  aren't flooded with suggestions from old activity. New activity flows from
  then on.
- Run a single `app` instance (the scheduler is in-process). Don't scale it to
  multiple replicas.
