# 🎬 Basecamp Butler

A self-hosted, zero-cost personal assistant for a **regular** Basecamp member.
It polls your Basecamp account every few minutes, tracks what's new across your
projects, and acts as a lightweight producer/coordinator: surfacing new
activity, suggesting to-dos, letting you confirm/dismiss them from a web UI or
straight from a Telegram push, and reminding you before things are due.

Built for **VFX / full-CG commercial production / DOOH** — the classifier is
seeded with the pipeline's vocabulary (render, comp, client review, loop, spec,
delivery, color grade, revision rounds…) rather than generic office terms.

- **No admin rights needed.** Uses OAuth as a normal member — polling only, no
  webhooks/SCIM. The API inherits whatever *you* can already see.
- **No paid anything.** Local Postgres, free Telegram bot, optional local Ollama
  LLM. No SaaS.

## Architecture

```
Basecamp REST ──poll every 5–10 min──▶ poller ──▶ Postgres ──▶ ┌ Web UI (FastAPI/:8000)
                                       (Python)   events/todos  ├ Telegram notifier
                                                                └ Classifier (rules | Ollama)
```

Everything runs from one `docker-compose.yml`: `db` (Postgres) + `app` (poller +
scheduler + web UI + notifier in a single process). Ollama is optional and
off by default.

## Quick start

1. **Register a Basecamp integration** (as yourself, no admin needed) at
   <https://launchpad.37signals.com/integrations>. Set the redirect URI to
   `http://localhost:8000/oauth/callback`. Note the Client ID/Secret.

2. **Create a Telegram bot** (optional but recommended): message
   [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token. Then
   message your new bot once and grab your numeric chat id from
   `https://api.telegram.org/bot<TOKEN>/getUpdates`.

3. **Configure**:
   ```bash
   cp .env.example .env
   # fill in BASECAMP_CLIENT_ID/SECRET, TELEGRAM_BOT_TOKEN/CHAT_ID
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
   - The repo is private, so toggle **Authentication** on and supply your GitHub
     username + a Personal Access Token with read access to the repo
     (github.com → Settings → Developer settings → *Fine-grained tokens* →
     Contents: Read). Or make the repo public to skip this.

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
   TELEGRAM_BOT_TOKEN=your-bot-token
   TELEGRAM_CHAT_ID=your-chat-id
   WEB_PORT=8000
   ```
   (`POLL_INTERVAL_MINUTES`, `DUE_SOON_DAYS`, `CLASSIFIER` have safe defaults.)

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

## How it decides what's a to-do (v1, rules)

Deterministic heuristics in [`app/classifier/rules.py`](app/classifier/rules.py):

- A **to-do assigned to you** → suggested to-do (with reminder if it has a due date).
- A **to-do due soon and unassigned** (within `DUE_SOON_DAYS`) → suggested to-do.
- A **message/comment that names you** → suggested to-do.
- A message/comment carrying an **action signal + pipeline term** (e.g. "please
  deliver the comp", "re-render the loop for the DOOH spec") → suggested to-do.

Every suggestion lands as `status = suggested` — never auto-confirmed — unless
you enable **auto-add** for that project on the Settings page, in which case it
lands as `confirmed`.

## Upgrading to the LLM classifier (v2, optional)

Set `CLASSIFIER=ollama` in `.env` and add an `ollama` service. The system prompt
frames the model as a senior VFX/CG producer-coordinator so its summaries and
suggestions use correct pipeline terminology. See
[`app/classifier/ollama.py`](app/classifier/ollama.py). Add to `docker-compose.yml`:

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

Each new suggestion/reminder is pushed to Telegram with inline **✅ Add** /
**✖ Dismiss** buttons. Tapping them updates the to-do's status in the DB — the
bot handles callbacks via long-polling, so no public webhook/URL is required.
Prefer [ntfy](https://ntfy.sh)? The notifier is isolated behind
`app/notifier/` and easy to swap.

## Data model

`projects`, `raw_events` (jsonb payloads), `todos`, `reminders`, `oauth_tokens`
(single row), plus `checkpoints` (per-type `updated_at` high-water mark) and
`app_state` (small kv). See [`app/models.py`](app/models.py). Tables are created
automatically on first boot.

## Env reference

| Var | Meaning |
|---|---|
| `BASECAMP_CLIENT_ID` / `_SECRET` | From your Launchpad integration |
| `BASECAMP_REDIRECT_URI` | Must match the integration; default `http://localhost:8000/oauth/callback` |
| `BASECAMP_USER_AGENT` | Basecamp requires a UA with contact info |
| `TELEGRAM_BOT_TOKEN` / `_CHAT_ID` | From @BotFather + getUpdates |
| `POLL_INTERVAL_MINUTES` | Poll cadence (default 7) |
| `DUE_SOON_DAYS` | "Due soon" threshold (default 3) |
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
