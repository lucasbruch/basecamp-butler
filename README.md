# 🎬 Basecamp Butler

A self-hosted, zero-cost personal assistant for a **regular** Basecamp member.
It polls your Basecamp account every few minutes, tracks what's new across your
projects, and acts as a lightweight coordinator: surfacing new activity,
suggesting to-dos, letting you confirm or dismiss them from a web UI or straight
from a phone push notification, and reminding you before things are due.

**Basic out of the box.** The default classifier reads generic work vocabulary
(documents, deliverables, tickets, meetings, deadlines), so it is useful for
anyone. Anything more specific is up to you: switch on the LLM classifier and
define a **persona** (character plus topics) that fits your own work, from the
Settings page.

- **No admin rights needed.** Uses OAuth as a normal member, polling only, no
  webhooks or SCIM. The API inherits whatever *you* can already see.
- **No paid anything.** Local Postgres, free push via ntfy (or a Telegram bot),
  optional local Ollama LLM. No SaaS.

This README is both the beginner walkthrough and the technical reference. If you
have never done this before, just follow **[Setup (0 to 100)](#setup-0-to-100)**
top to bottom. Every value you fill in yourself is shown in `<angle brackets>`.

## Architecture

```
Basecamp REST ──poll every ~5 min──▶ poller ──▶ Postgres ──▶ ┌ Web UI (FastAPI/:8000)
                                     (Python)   events/todos  ├ Notifier (ntfy | Telegram)
                                                              └ Classifier (rules | Ollama)
```

Everything runs from one `docker-compose.yml`: `db` (Postgres) plus `app` (poller,
scheduler, web UI, and notifier in a single process). Ollama is optional and off
by default.

## Setup (0 to 100)

This is the step-by-step path from nothing to a Butler running 24/7 on a Synology
NAS, reachable from your phone, and optionally reachable from a laptop on a
locked-down company VPN.

### What you get

- Basecamp Butler running 24/7 on your NAS.
- Push notifications on your phone for anything that looks like a task for you.
- A web dashboard to confirm, dismiss, or complete those tasks.
- (Optional) An AI classifier (Ollama) that understands your specific kind of work.
- (Optional) Secure access from anywhere via Tailscale, even behind a company VPN.

### Before you start

1. A Synology NAS with **Portainer** installed. (Portainer is a free add-on that
   manages Docker stacks. Container Manager works too, but this guide uses
   Portainer.)
2. Your own Basecamp login. You do **not** need to be an account admin.
3. A phone, for push notifications.
4. About 30 minutes.

You do not need to buy anything. Every piece here is free.

### Step 1: Register a Basecamp integration

This gives the app permission to read your Basecamp on your behalf. It is tied to
your personal login, so no admin approval is required.

1. Go to <https://launchpad.37signals.com/integrations>.
2. Click **Register another application**.
3. Fill in:
   - **Name**: anything, for example `Basecamp Butler`.
   - **Company or website**: anything, for example your name.
   - **Redirect URI**: type this exactly, replacing the placeholder with the LAN
     IP of your NAS:
     ```
     http://<nas-lan-ip>:8000/oauth/callback
     ```
4. Save. You now see a **Client ID** and a **Client Secret**. Keep this page open;
   you paste both into Portainer in Step 3. Treat the secret like a password.

> If you do not know your NAS LAN IP: open Synology DSM, go to
> **Control Panel > Network > Network Interface**, and read the IPv4 address.

### Step 2: Set up phone notifications (ntfy)

ntfy is a free push service with no account and no bot to create.

1. Install the **ntfy** app on your phone (App Store or Google Play), or use
   <https://ntfy.sh> in a browser.
2. In the app, **subscribe to a new topic**. Pick a long, hard-to-guess name,
   because anyone who knows the topic name can read those notifications. For
   example `basecamp-butler-<some-random-letters-and-numbers>`.
3. Write that exact topic name down. You paste it into Portainer as `NTFY_TOPIC`
   in the next step.

That is all. No password, no sign-up.

> Prefer Telegram? Set `NOTIFY_CHANNEL=telegram` and provide a @BotFather bot
> token plus your chat id instead of the ntfy topic.

### Step 3: Deploy on the NAS with Portainer

Portainer downloads this project from GitHub, builds it, and runs it. You never
touch the command line.

1. Open Portainer, go to **Stacks**, click **Add stack**.
2. Give it a name, for example `basecamp-butler`.
3. Choose **Repository** as the build method and fill in:
   - **Repository URL**: `https://github.com/lucasbruch/basecamp-butler`
   - **Reference**: `refs/heads/main`
   - **Compose path**: `docker-compose.yml`
   - Leave **Authentication** off (the repo is public). If you fork it private,
     turn Authentication on and supply your GitHub username plus a fine-grained
     PAT with Contents: Read.
4. Scroll to **Environment variables** and add the following. Replace every
   `<...>` with your own value. The compose reads these via `${VAR}` substitution.
   ```
   POSTGRES_USER=basecamp
   POSTGRES_PASSWORD=<pick-a-password>
   POSTGRES_DB=basecamp
   BASECAMP_CLIENT_ID=<from Step 1>
   BASECAMP_CLIENT_SECRET=<from Step 1>
   BASECAMP_REDIRECT_URI=http://<nas-lan-ip>:8000/oauth/callback
   BASECAMP_USER_AGENT=BasecampButtler (<your-email>)
   NOTIFY_CHANNEL=ntfy
   NTFY_TOPIC=<from Step 2>
   APP_BASE_URL=http://<nas-lan-ip>:8000
   WEB_PORT=8000
   ```
   (`NTFY_SERVER`, `POLL_INTERVAL_MINUTES`, `DUE_SOON_DAYS`, and `CLASSIFIER` have
   safe defaults. `APP_BASE_URL` makes the notification buttons work; set it to
   the same address you use to reach the app.)

   > **Important gotcha:** `POSTGRES_PASSWORD` must contain only letters and
   > numbers (dashes and underscores are fine too). Symbols like `@`, `:`, or `/`
   > break the internal database address and the app returns a blank page. If you
   > ever change this password later, you must also delete the database volume,
   > because the password is locked in when the database is first created.

5. Click **Deploy the stack** and wait for the first build to finish (a few
   minutes). Optionally enable **Automatic updates** or the redeploy **webhook**
   so a future `git push` rolls out on its own.

> No-git alternative: [`deploy/portainer-stack.yml`](deploy/portainer-stack.yml)
> references a pre-built `basecamp-butler:latest` image if you would rather build
> it once over SSH than let Portainer build.

### Step 4: Connect your Basecamp account

1. In a browser on the same network as the NAS, open:
   ```
   http://<nas-lan-ip>:8000
   ```
   You should see the Butler dashboard.
2. Go to the **Settings** page, click **Connect Basecamp**, approve, and you are
   returned to the app. (No `authorize.py` needed; that is only for a desktop
   setup, see [Run locally instead](#run-locally-instead).)

From now on the app polls Basecamp every few minutes.

> **What to expect at first:** the very first poll only records a starting point.
> It does **not** import old history, on purpose, so you are not flooded with
> stale suggestions. New activity from this moment forward is what shows up. So if
> the dashboard looks empty right after setup, that is correct. Give it a little
> real activity.

At this point you have a fully working Butler on your home network. The next two
steps are optional upgrades.

### Step 5 (optional): Turn on the AI classifier with Ollama

By default the app uses simple keyword rules (see
[How it decides](#how-it-decides-whats-a-to-do-v1-rules)). If you want it to
understand your specific field, switch on Ollama, a free local AI. No cloud, no
cost. There are two ways to run it.

**Option A, run Ollama on the NAS as a container.** Add this service to
`docker-compose.yml` (or your Portainer stack) and set `CLASSIFIER=ollama`:

```yaml
  ollama:
    image: ollama/ollama:latest
    restart: unless-stopped
    volumes:
      - ollama:/root/.ollama
# and add `ollama:` under top-level volumes, then pull a model once:
#   docker compose exec ollama ollama pull llama3.1:8b
```

With this option leave `OLLAMA_URL` at its default (`http://ollama:11434`), the
containers talk to each other directly.

**Option B, run Ollama on a separate computer** (for example a desktop with a
GPU):

1. Install Ollama from <https://ollama.com> on that computer and pull a model:
   ```
   ollama pull llama3.1:8b
   ```
2. Make Ollama accept connections from the NAS, not just from itself. Set a system
   environment variable `OLLAMA_HOST=0.0.0.0`, then fully quit Ollama (from the
   tray icon) and start it again so it picks up the change.
3. In Portainer set `CLASSIFIER=ollama` and
   `OLLAMA_URL=http://<ollama-computer-ip>:11434`, then redeploy.

Either way, open the **Settings** page afterward. You can give the assistant a
**persona** (a role and the topics it should watch for, or a full system-prompt
override) and press **Test it** to run a sample message through it live. Overrides
are stored in `app_state`, so they take effect without a restart. See
[`app/classifier/ollama.py`](app/classifier/ollama.py).

If the Ollama computer sometimes joins a company VPN, read Step 6, which solves
the "it disappears when I connect the VPN" problem.

### Step 6 (optional): Reach it from anywhere, even on a corporate VPN

The problem: a full-tunnel, IT-managed company VPN takes over all network routing
on your laptop the moment it connects. While connected, your laptop can no longer
reach the NAS by its home IP, and if Ollama runs on that laptop, the NAS can no
longer reach Ollama either. You usually cannot change a company-managed VPN, so
the fix has to work on top of it.

The solution: **[Tailscale](https://tailscale.com)**, a free private network
(WireGuard mesh) that gives every device a fixed `100.x` address that keeps
working no matter what network you are on. It is encrypted end to end, needs no
port forwarding, and exposes nothing to the public internet. It rides over the
VPN's own connection (relaying over HTTPS when direct traffic is blocked), so it
keeps working *while* the VPN is on.

1. Create a free account at <https://tailscale.com>.
2. Install Tailscale on the **NAS**. On Synology this is in **Package Center**:
   search **Tailscale**, install, open, and sign in.
3. Install Tailscale on the **laptop** (and on your **phone** if you want the
   notification buttons to work while away from home). Sign in to the same account.
4. In the Tailscale admin console, note the `100.x` address of the NAS and, if you
   use Option B in Step 5, of the Ollama computer.
5. Use those `100.x` addresses instead of home IPs:
   - Open the app at `http://<nas-tailscale-ip>:8000`.
   - For NAS to Ollama, set `OLLAMA_URL=http://<ollama-tailscale-ip>:11434` in
     Portainer and redeploy.
   - To make the phone notification buttons work from anywhere, set
     `APP_BASE_URL=http://<nas-tailscale-ip>:8000` and redeploy.
6. Tip: turn on **MagicDNS** in the admin console, then you can use simple names
   like `http://<nas-name>:8000` instead of the numeric address.

**Lock down the Ollama port** if you used Option B. On the Ollama computer, in an
administrator terminal (Windows PowerShell example):

```powershell
New-NetFirewallRule -DisplayName "Ollama tailnet only" -Direction Inbound -Protocol TCP -LocalPort 11434 -RemoteAddress 100.64.0.0/10 -Action Allow
```

`100.64.0.0/10` is the range Tailscale uses, so this allows only your own private
network and blocks everything else.

**If the NAS runs Tailscale in userspace mode (common on Synology).** A Synology
without `/dev/net/tun` runs the Tailscale package in userspace mode: `tailscaled`
can see the tailnet, but the host kernel has no route to `100.x`, so your Docker
container cannot reach a tailnet Ollama even though the NAS "has Tailscale." You
can check with `ls -l /dev/net/tun` (missing) plus a host `curl` to the Ollama
node failing instantly while `tailscale status` still lists it. The fix is the
bundled **Tailscale sidecar**: it joins your tailnet in userspace mode (no TUN, no
privileges) and exposes an HTTP proxy that the app routes its Ollama calls
through. It ships in `docker-compose.yml` and stays inert until you give it an
auth key. In Portainer set:

```
TS_AUTHKEY=<from Tailscale admin console: Settings → Keys>
OLLAMA_PROXY=http://tailscale:1055
OLLAMA_URL=http://<ollama-tailnet-ip>:11434
CLASSIFIER=ollama
```

Then **Pull and redeploy**. You should now see a `tailscale` container running
alongside `db` and `app`. Only the Ollama calls use the proxy; Basecamp polling
and the database connection are untouched. The new tailnet node appears in your
admin console under `TS_HOSTNAME` (default `basecamp-butler`).

> A few very strict company VPNs block all non-VPN traffic, which can also block
> Tailscale. If nothing above works while the VPN is on: expose only the web UI
> via an outbound **Cloudflare Tunnel** on the NAS (gated to your login with
> Cloudflare Access), and move Ollama off the VPN laptop onto an always-on machine
> at home or the NAS itself, so the NAS to Ollama path never depends on the VPN.

### Step 7: Lock down the web interface

By default the UI is open to anyone on your network. Add a password so it is
protected even inside your private Tailscale network.

1. In Portainer add this environment variable and pick a long random value:
   ```
   WEB_AUTH_TOKEN=<pick-a-long-random-string>
   ```
2. Redeploy. The browser now asks for a login: enter any username and use your
   token as the password. The phone notification buttons send it automatically.

Strongly recommended if the app is reachable beyond a trusted home network.

### Run locally instead

If you would rather run on a desktop for development instead of a NAS:

```bash
cp .env.example .env
# fill in BASECAMP_CLIENT_ID/SECRET and NTFY_TOPIC (or the Telegram vars)

# authorize Basecamp once (interactive OAuth handshake, stores tokens in the DB):
docker compose run --rm --service-ports app python scripts/authorize.py

docker compose up -d
# open http://localhost:8000
```

Use `http://localhost:8000/oauth/callback` as the redirect URI in your Basecamp
integration for this path.

## How it decides what's a to-do (v1, rules)

Deterministic heuristics in [`app/classifier/rules.py`](app/classifier/rules.py):

- A **to-do assigned to you** becomes a suggested to-do (with a reminder if it has
  a due date).
- A **to-do due soon and unassigned** (within `DUE_SOON_DAYS`) becomes a suggested
  to-do.
- A **message, comment, or chat line that names you** becomes a suggested to-do.
- A message, comment, or chat carrying an **action signal plus a work noun** (for
  example "please send the budget", "can you review the deck before Friday")
  becomes a suggested to-do.
- A **Ping (direct message)** that reads like an ask becomes a suggested to-do,
  tagged with the sender. Pings are higher-signal (aimed at you), so either gate
  is enough.
- A **calendar event you're a participant in** becomes a to-do with the start
  time as its due date. Documents and uploads only qualify if they name you or
  read as a request — most of what lands in Docs & Files is FYI.

Two things keep the noise down:

- **One suggestion per conversation.** While a chat thread has an open to-do,
  further messages in it won't raise another one (window set by *Coalesce chat
  bursts* on Settings). Once you dismiss or complete it, the thread is clear
  again.
- **Muted senders.** Name the deploy bot on the Settings page and its messages
  stop counting entirely.

### What it reads

To-dos, message-board posts, comments, **calendar events**, **documents**,
**uploads**, **Campfire** chat lines, and **Pings** (1:1 or small-group DMs).
Pings are not in the projects or recordings index; they are pulled from the
account notifications feed (`/my/readings.json`, `section: pings`) and read via
the same chat-lines endpoint as Campfire. Toggle the last two with
`POLL_CAMPFIRE` and `POLL_PINGS`. Everything respects your Basecamp visibility.

Every suggestion lands as `status = suggested`, never auto-confirmed, unless you
enable **auto-add** for that project on the Settings page, in which case it lands
as `confirmed`.

## Web UI

- **Dashboard** (`/`): active to-dos with a health strip showing last-poll status,
  so you can tell at a glance if polling is stuck. Snoozed items are tucked away
  until they're due back.
- **To-dos** (`/todos`): review, confirm, dismiss, mark done, or snooze — with
  search, filters and multi-select for clearing a burst in one go.
- **Replies** (`/replies`): drafted answers to Pings waiting for you to read,
  edit and send, plus everything that has already gone out.
- **Report** (`/report`): an on-demand briefing over a window you pick, plus the
  archive of earlier ones.
- **Activity** (`/activity`): a searchable, plain-English trace of everything
  ingested and every decision made, whether or not it became a to-do.
- **Settings** (`/settings`): connect Basecamp, edit the behaviour settings
  below, pick write-back targets, mute senders, choose who may be auto-replied to
  and in what tone, and shape the assistant persona.

Buttons act in place rather than reloading the page, and the pages only refresh
themselves when the server says something actually changed. Light and dark themes
both follow your system setting.

**Keyboard:** <kbd>j</kbd>/<kbd>k</kbd> to move between cards, <kbd>a</kbd> add,
<kbd>d</kbd> dismiss, <kbd>e</kbd> done, <kbd>s</kbd> snooze an hour.

## Settings you can change without redeploying

Anything on the **Settings** page takes effect immediately — the poll interval,
the classifier, the notification channel, the time zone, quiet hours, retention,
and the daily briefing. Each falls back to its environment variable until you
change it, and setting it back to the environment value clears the override
rather than pinning it.

Credentials (client id/secret, ntfy topic, bot token) stay in the environment.
They're secrets, not preferences, and still need a redeploy.

## Snooze, quiet hours and digests

- **Snooze** a suggestion for an hour, three hours, tomorrow morning, or next
  week. It disappears from the dashboard and comes back with a nudge when it's
  due. Available in the UI, by keyboard, and from the Telegram buttons.
- **Quiet hours** (default 22:00–07:00 local) suppress individual pushes.
  Nothing is lost: whatever came up is delivered as a single digest when the
  window ends, minus anything you already dealt with in the meantime.
- **Digests** collapse a cycle that raises more than *N* suggestions into one
  notification instead of a burst.

Everything is stored in UTC and rendered in `TIMEZONE`, so quiet hours, the
daily briefing and the snooze landmarks ("tomorrow morning" = 09:00) all follow
the wall clock where you are, on both sides of a daylight-saving change. The one
exception is a to-do's due date: Basecamp gives that as a bare calendar day, so
it names the same day in every zone rather than being shifted by an offset.

## Daily briefing

Turn it on in Settings and the app generates the same report the `/report` page
produces and pushes it at a local hour you choose. Quiet periods are skipped
rather than sent as "nothing happened". Every briefing — scheduled or on demand
— is archived and re-readable from `/report`.

## Writing back to Basecamp

By default the butler is read-only: ✅ confirms the to-do in its own database.
Switch on **write-back** in Settings and pick a target to-do list per project,
and confirming also creates the real Basecamp to-do, so the work appears where
your colleagues actually look.

It's deliberately opt-in twice (the global switch *and* a per-project target),
never runs twice for the same suggestion, and can only touch what your own login
can already reach. A failed write leaves the to-do confirmed locally and explains
itself in the activity feed.

## Replying to Pings

The butler can also answer your direct messages. This is the one thing it does
that other people see, so it is fenced in on every side:

- **Off by default** (`AUTOREPLY_ENABLED`, or the switch in Settings).
- **Allowlist only.** On the Settings page you add the people it may answer, one
  by one, by their Basecamp display name. There is no "reply to everyone".
  Anyone not on the list is never answered, and **Campfire group chat is never
  answered at all** — a room full of people is not a conversation you can safely
  autopilot.
- **A tone per person.** Each entry carries free-text tone ("warm and casual,
  first names, the odd emoji") and optional standing instructions ("never commit
  to a date"). A **Draft a reply** button on Settings lets you hear a tone
  against a made-up message before anyone real receives it.
- **Draft or auto, per person.** *Draft* writes the reply and parks it on the
  **Replies** page for you to edit and send; *auto* posts it straight away.
  New entries default to draft — start there for a week.
- **Bounded, but nothing is dropped.** One reply per conversation per cooldown
  window, one unread draft per conversation at a time, and a hard daily ceiling.
  A message that arrives while one of those applies is *held*, not discarded —
  it gets answered once the window passes or you clear the draft. (Anything left
  unanswered for more than six hours is then let go: turning up half a day late
  in someone's inbox, in your name, is worse than staying quiet.)
- **Quiet hours decide when, not whether.** An *auto* reply composed during
  quiet hours is held and sent when the window ends. One held back by the daily
  ceiling stays a draft — a ceiling that empties itself an hour later isn't one.
- **No backlog.** Switching it on records where each conversation currently
  stands and answers nothing older, so nobody gets a volley of replies to
  last week's messages.
- **Never the last word.** If the newest message in the thread is already yours,
  there is nothing to answer.
- **Never twice.** A model can only see the conversation, and once a thread has
  moved on to "haha" the most obvious thing in its context is the line it wrote
  itself an hour ago — so it hands that back. Wording already in a conversation
  is never posted into it again, whichever way the pass arrived at it, and a
  send is committed the moment Basecamp accepts it so a later failure can't
  cause the same message to go out a second time.

Replies are written by the local LLM, so this needs a reachable Ollama —
independently of which classifier you're running. If the LLM host is asleep, or
answers a 5xx because the model is still loading, the thread is left alone and
reconsidered next cycle; silence is always the safe outcome.

A reply pass runs every minute on its own timer, not only at the tail of a poll,
so a failed Basecamp fetch doesn't cost you a cycle. **Check for new Pings now**
on /replies runs one immediately.

**Why it is quiet**, on the same page, is the answer to "it didn't reply — why
not?". It carries the decision it reached about every conversation it looked at
("Ana isn't on the auto-reply list", "already answered inside the 30-minute
window", "the model read it and judged no reply was needed"), the summary of the
last pass, and the two heartbeats upstream of it — when Pings were last fetched,
and whether the LLM is answering. Those last two matter because a butler that
never saw the message and one that saw it and chose to stay out of it are
completely different problems. It is a status board, not a log: one line per
conversation, overwritten each pass, kept for a week.

Every reply it composes — drafted, sent, discarded or failed — is kept and listed
on **/replies** with its exact text, so "what has it said in my name?" has one
answer.

## Notifications

Set the channel with `NOTIFY_CHANNEL` or the Settings page (`ntfy`, `telegram`,
or `none`). Each new suggestion or reminder is pushed with **✅ Add / ✖ Dismiss /
Open** action buttons (a confirmed to-do shows **✔ Done** instead of Add).

- **ntfy (default):** push to `NTFY_SERVER/NTFY_TOPIC`, no account, no bot. The
  buttons POST back to this app's `/api/todos/{id}/{action}` routes, so set
  `APP_BASE_URL` to an address your phone can reach (same LAN, or via
  [Tailscale](https://tailscale.com) when away from home). Without `APP_BASE_URL`
  you still get notifications, just no buttons.
- **telegram:** inline buttons handled via bot long-polling (works anywhere, no
  public URL needed). Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Telegram
  has room for a second button row, so it also carries the snooze presets.

Both live behind `app/notifier/`; adding another channel is a small module.

## Learning from your decisions

With the LLM classifier on, every ✅ and ✖ becomes a worked example. The most
recent kept and rejected titles are appended to the system prompt, so the
assistant drifts toward your idea of a real task instead of repeating the same
false positive weekly. You can see exactly what it has learned on the Settings
page under the persona editor.

## Updating to a new version later

Because Portainer builds from GitHub, updating is simple:

1. In Portainer, open the stack and click **Pull and redeploy** (or just
   `git push` if you enabled automatic updates).
2. Hard-refresh the browser (Ctrl+Shift+R) so you are not looking at a cached page.

Your database, your saved tasks, and your Basecamp login all survive a redeploy.
Schema changes run automatically on boot (`alembic upgrade head`), so there is
nothing to do by hand.

> If a change does not seem to show up, it is almost always one of two things: the
> new version was not pulled and redeployed, or the browser is showing a cached
> page. Do both actions above and check again.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Blank page after deploy | `POSTGRES_PASSWORD` has a symbol in it. Use only letters and numbers, then delete the database volume and redeploy (see the Step 3 gotcha). |
| Dashboard empty right after setup | Normal. The first poll only sets a starting point and does not import old history. Wait for new activity. |
| "Test it" on Settings fails | `OLLAMA_URL` is wrong, Ollama is not running, Ollama is still bound to itself only (set `OLLAMA_HOST=0.0.0.0` and restart it), or a firewall rule blocks the NAS. |
| Cannot reach the app on the company VPN | Use the Tailscale address from Step 6, not the home IP. |
| Changes do not appear after an update | Pull and redeploy in Portainer, then hard-refresh the browser. |
| No push notifications | Check the `NTFY_TOPIC` in Portainer matches the topic your phone is subscribed to, exactly. |

## Data model

`projects`, `raw_events` (jsonb payloads), `todos`, `reminders`, `reports`,
`muted_senders`, `activity_log`, `autoreply_rules` (who may be answered) and
`autoreplies` (every reply composed), `oauth_tokens` (single row), plus `checkpoints`
(per-type `updated_at` high-water mark) and `app_state` (small kv, which also
holds the Settings-page overrides under a `cfg_` prefix). See
[`app/models.py`](app/models.py). The schema is managed by **Alembic**
([`migrations/`](migrations)): the app runs `alembic upgrade head` on boot,
falling back to `create_all()` for a fresh install if that fails.

**Retention.** `raw_events` grows by one row per chat line, forever, so a sweep
runs each poll: processed events older than `RAW_EVENT_RETENTION_DAYS` and
resolved to-dos older than `TODO_RETENTION_DAYS` are deleted in bounded batches.
Open to-dos, events still queued for the classifier, and events a live to-do
points at are never touched. Set either to 0 to disable that sweep.

## Env reference

| Var | Meaning |
|---|---|
| `BASECAMP_CLIENT_ID` / `_SECRET` | From your Launchpad integration |
| `BASECAMP_REDIRECT_URI` | Must match the integration; default `http://localhost:8000/oauth/callback` |
| `BASECAMP_USER_AGENT` | Basecamp requires a UA with contact info |
| `NOTIFY_CHANNEL` | `ntfy` (default), `telegram`, or `none` |
| `NTFY_SERVER` / `NTFY_TOPIC` | ntfy server (default `https://ntfy.sh`) plus your topic |
| `NTFY_TOKEN` | Optional, for protected or self-hosted ntfy topics |
| `APP_BASE_URL` | This app's reachable URL; powers notification buttons |
| `TELEGRAM_BOT_TOKEN` / `_CHAT_ID` | Only if `NOTIFY_CHANNEL=telegram` |
| `POLL_INTERVAL_MINUTES` | Poll cadence (default 5) |
| `DUE_SOON_DAYS` | "Due soon" threshold (default 3) |
| `POLL_CAMPFIRE` / `POLL_PINGS` | Ingest Campfire chat / Pings (both default `true`) |
| `WEB_AUTH_TOKEN` | Optional secret to lock the UI and API behind HTTP Basic (blank means open, LAN-only) |
| `CLASSIFIER` | `rules` (default) or `ollama` |
| `OLLAMA_URL` / `OLLAMA_MODEL` | For the v2 classifier |
| `OLLAMA_PROXY` | Optional outbound proxy for Ollama calls only, e.g. `http://tailscale:1055` (blank means direct) |
| `TIMEZONE` | IANA zone for display, quiet hours and the daily report (default `UTC`). Spelled exactly as in the zone database — `Europe/Berlin`; an unknown name logs a warning and falls back to `UTC`. The Settings page offers this as a dropdown, so it's only worth setting here to change the default |
| `QUIET_HOURS_START` / `_END` | Local hours between which nothing is pushed (default 22 → 7; equal values disable) |
| `DIGEST_THRESHOLD` | Collapse a cycle into one push above this many suggestions (default 3; 0 disables) |
| `THREAD_COALESCE_HOURS` | Suppress a second suggestion while a chat thread has an open one (default 6; 0 disables) |
| `DAILY_REPORT_ENABLED` / `_HOUR` / `_HOURS` | Scheduled briefing: on/off, local hour, and window size (default off, 08:00, 24h) |
| `WRITEBACK_ENABLED` | Create real Basecamp to-dos on confirm (default off; also needs a per-project target list) |
| `AUTOREPLY_ENABLED` | Answer Pings from people on the allowlist (default off; also needs at least one person listed in Settings) |
| `AUTOREPLY_COOLDOWN_MINUTES` | At most one reply per conversation in this window (default 30; 0 disables). Messages arriving inside it are answered when it passes, not skipped |
| `AUTOREPLY_DAILY_LIMIT` | Ceiling on replies sent per rolling 24h; beyond it an *auto* rule drafts instead (default 10) |
| `RAW_EVENT_RETENTION_DAYS` / `TODO_RETENTION_DAYS` | Housekeeping windows (default 90 / 180; 0 disables) |
| `TS_AUTHKEY` / `TS_HOSTNAME` / `TS_EXTRA_ARGS` | Tailscale sidecar auth key (blank = sidecar idle), tailnet node name (default `basecamp-butler`), and extra `tailscaled` flags |

Everything from `TIMEZONE` down — plus the interval, classifier, due-soon window
and channel — is also editable on the **Settings** page, and changes there take
effect without a redeploy.

## Notes & limits

- Access tokens expire roughly every 2 weeks; the poller auto-refreshes using the
  stored refresh token before each run.
- Rate limit is 50 requests per 10 seconds per token; the client throttles and
  honours `Retry-After` on 429.
- The first poll only **seeds** the checkpoints (no historical backfill) so you
  are not flooded with suggestions from old activity. New activity flows from then
  on.
- Run a single `app` instance (the scheduler is in-process). Do not scale it to
  multiple replicas.
