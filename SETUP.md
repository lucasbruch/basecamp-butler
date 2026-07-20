# Basecamp Butler: Complete Setup Guide (0 to 100)

This is the step-by-step, no-experience-needed guide to get Basecamp Butler
running on a Synology NAS and reachable from anywhere, including from a laptop on
a locked-down company VPN. Follow it top to bottom. Every value you have to fill
in yourself is shown in `<angle brackets>`, so nothing private is baked in.

If you just want the short technical reference instead, see the [README](README.md).

## What you will end up with

- Basecamp Butler running 24/7 on your NAS.
- Push notifications on your phone for anything that looks like a task for you.
- A web dashboard to confirm, dismiss, or complete those tasks.
- (Optional) An AI classifier (Ollama) that understands your specific kind of work.
- (Optional) Secure access from anywhere via Tailscale, even behind a corporate VPN.

## What you need before you start

1. A Synology NAS with **Container Manager** or **Portainer** installed.
   (This guide uses Portainer. It is a free add-on that manages Docker stacks.)
2. Your own Basecamp login. You do **not** need to be an account admin.
3. A phone, for push notifications.
4. About 30 minutes.

You do not need to buy anything. Every piece here is free.

---

## Step 1: Register a Basecamp integration

This gives the app permission to read your Basecamp on your behalf. It is tied to
your personal login, so no admin approval is required.

1. Go to <https://launchpad.37signals.com/integrations>.
2. Click **Register another application** (or **New**).
3. Fill in:
   - **Name**: anything, for example `Basecamp Butler`.
   - **Company / website**: anything, for example your name.
   - **Redirect URI**: type this exactly, replacing the placeholder with the LAN
     IP of your NAS:
     ```
     http://<nas-lan-ip>:8000/oauth/callback
     ```
     Example shape only: `http://192.168.x.x:8000/oauth/callback`.
4. Save. You now see a **Client ID** and **Client Secret**. Keep this page open,
   you will paste both into Portainer in Step 3. Treat the secret like a password.

> If you do not know your NAS LAN IP: open the Synology DSM, go to
> **Control Panel > Network > Network Interface**, and read the IPv4 address.

---

## Step 2: Set up phone notifications (ntfy)

ntfy is a free push service with no account and no bot to create.

1. Install the **ntfy** app on your phone (App Store or Google Play), or use
   <https://ntfy.sh> in a browser.
2. In the app, **subscribe to a new topic**. Pick a long, hard-to-guess name,
   because anyone who knows the topic name can read those notifications. For
   example: `basecamp-butler-<some-random-letters-and-numbers>`.
3. Write that exact topic name down. You will paste it into Portainer as
   `NTFY_TOPIC` in the next step.

That is all. No password, no sign-up.

---

## Step 3: Deploy on the NAS with Portainer

Portainer will download this project from GitHub, build it, and run it. You never
touch the command line.

1. Open Portainer, go to **Stacks**, click **Add stack**.
2. Give it a name, for example `basecamp-butler`.
3. Choose **Repository** as the build method and fill in:
   - **Repository URL**: `https://github.com/lucasbruch/basecamp-butler`
   - **Reference**: `refs/heads/main`
   - **Compose path**: `docker-compose.yml`
   - Leave **Authentication** off (the repo is public).
4. Scroll to **Environment variables** and add the following. Replace every
   `<...>` with your own value.
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

   > **Important gotcha:** `POSTGRES_PASSWORD` must contain only letters and
   > numbers (dashes and underscores are fine too). Symbols like `@`, `:`, or `/`
   > break the internal database address and the app will return a blank page. If
   > you ever change this password later, you must also delete the database volume,
   > because the password is locked in when the database is first created.

5. Click **Deploy the stack** and wait for it to build. The first build takes a
   few minutes.

---

## Step 4: Connect your Basecamp account

1. In a browser on the same network as the NAS, open:
   ```
   http://<nas-lan-ip>:8000
   ```
   You should see the Butler dashboard.
2. Go to the **Settings** page (link in the top navigation).
3. Click **Connect Basecamp**, approve the request, and you are returned to the app.

That is the whole connection. From now on the app polls Basecamp every few minutes.

> **What to expect at first:** the very first poll only records a starting point.
> It does **not** import old history, on purpose, so you are not flooded with
> hundreds of stale suggestions. New activity from this moment forward is what
> shows up. So if the dashboard looks empty right after setup, that is correct.
> Give it a little real activity.

At this point you have a fully working Butler on your home network. The next two
steps are optional upgrades.

---

## Step 5 (optional): Turn on the AI classifier with Ollama

By default the app uses simple keyword rules to decide what is a task. That works
for everyone. If you want it to understand your specific field, switch on Ollama,
a free local AI that runs on your own computer. No cloud, no cost.

You will run Ollama on a computer that stays on when you need classification (a
desktop or your laptop) and point the NAS at it.

1. Install Ollama from <https://ollama.com> on that computer.
2. Download a model. Open a terminal on that computer and run:
   ```
   ollama pull llama3.1:8b
   ```
3. Make Ollama accept connections from the NAS, not just from itself. Set a
   system environment variable on that computer:
   ```
   OLLAMA_HOST=0.0.0.0
   ```
   Then fully quit Ollama (from the tray icon) and start it again, so it picks up
   the change.
4. In Portainer, edit the stack environment variables, set these two, and redeploy:
   ```
   CLASSIFIER=ollama
   OLLAMA_URL=http://<ollama-computer-ip>:11434
   ```
5. Open the **Settings** page in the Butler UI. You can give the assistant a
   **persona** (a role and the topics it should watch for) and press **Test it**
   to run a sample message through it live. No code changes, it saves instantly.

If the Ollama computer and the NAS are on the same home network, use that
computer's normal LAN IP for `OLLAMA_URL`. If that computer sometimes joins a
company VPN, read Step 6, which solves the "it disappears when I connect the VPN"
problem.

---

## Step 6 (optional): Reach it from anywhere, even on a corporate VPN

The problem: a company VPN often takes over all network routing on your laptop.
While it is connected, your laptop can no longer reach the NAS by its home IP, and
if Ollama runs on that laptop, the NAS can no longer reach Ollama either. You
usually cannot change a company-managed VPN, so the fix has to work on top of it.

The solution: **Tailscale**, a free private network that gives every device a
fixed address that keeps working no matter what network you are on. It is
encrypted end to end and exposes nothing to the public internet.

1. Create a free account at <https://tailscale.com> (sign in with any account you
   like).
2. Install Tailscale on the **NAS**. On Synology this is in **Package Center**:
   search for **Tailscale**, install, open it, and sign in.
3. Install Tailscale on the **laptop** (and on your **phone** if you want the
   notification buttons to work while away from home). Sign in to the same account.
4. Open the Tailscale admin console. Each device now has a fixed address that
   starts with `100.`. Note the address for the NAS and, if you use Ollama, for
   the Ollama computer.
5. Use those `100.` addresses instead of home IPs:
   - Open the app at `http://<nas-tailscale-ip>:8000`.
   - In Portainer, set `OLLAMA_URL=http://<ollama-tailscale-ip>:11434` and redeploy.
   - If you want the phone notification buttons to work from anywhere, set
     `APP_BASE_URL=http://<nas-tailscale-ip>:8000` and redeploy.
6. Tip: turn on **MagicDNS** in the Tailscale admin console. Then you can use
   simple names like `http://<nas-name>:8000` instead of the numeric address.

### Lock down the Ollama port (do this if you did Step 5)

You opened Ollama to the network in Step 5. Restrict it so only your Tailscale
devices can reach it. On the Ollama computer, in an administrator terminal
(Windows PowerShell example):

```powershell
New-NetFirewallRule -DisplayName "Ollama tailnet only" -Direction Inbound -Protocol TCP -LocalPort 11434 -RemoteAddress 100.64.0.0/10 -Action Allow
```

`100.64.0.0/10` is the range Tailscale uses, so this allows only your own private
network and blocks everything else.

### If even Tailscale is blocked

A few very strict company VPNs block all non-VPN network traffic, which can also
block Tailscale. If nothing above works while the VPN is on:

- For the web UI only, use a **Cloudflare Tunnel** on the NAS. It makes an
  outbound-only secure connection, so it works from any network, and you can gate
  it to your own login with Cloudflare Access.
- Move Ollama off the VPN laptop onto an always-on machine at home, or run it on
  the NAS itself, so the NAS to Ollama path never depends on the VPN.

---

## Step 7: Lock down the web interface

By default the UI is open to anyone on your network. Add a password so it is
protected even inside your private Tailscale network.

1. In Portainer, add this environment variable and pick a long random value:
   ```
   WEB_AUTH_TOKEN=<pick-a-long-random-string>
   ```
2. Redeploy.
3. Now the browser asks for a login. Enter any username and use your token as the
   password. The phone notification buttons send it automatically.

Strongly recommended if the app is reachable beyond a trusted home network.

---

## Updating to a new version later

Because Portainer builds from GitHub, updating is simple:

1. In Portainer, open the stack and click **Pull and redeploy**.
2. Hard-refresh the browser (Ctrl+Shift+R) so you are not looking at a cached page.

Your database, your saved tasks, and your Basecamp login all survive a redeploy.

> If a change "does not seem to show up," it is almost always one of two things:
> the new version was not pulled and redeployed, or the browser is showing a
> cached page. Do both actions above and check again.

---

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Blank page after deploy | `POSTGRES_PASSWORD` has a symbol in it. Use only letters and numbers, then delete the database volume and redeploy (see Step 3 gotcha). |
| Dashboard is empty right after setup | Normal. The first poll only sets a starting point and does not import old history. Wait for new activity. |
| "Test it" on Settings fails | `OLLAMA_URL` is wrong, Ollama is not running, Ollama is still bound to itself only (`OLLAMA_HOST` not applied, restart it), or the firewall rule blocks the NAS. |
| Cannot reach the app on the company VPN | Use the Tailscale address from Step 6, not the home IP. |
| Changes do not appear after an update | Pull and redeploy in Portainer, then hard-refresh the browser (see Updating). |
| No push notifications | Check the `NTFY_TOPIC` in Portainer matches the topic your phone is subscribed to, exactly. |

---

## Where things live (for the curious)

- All configuration is environment variables in the Portainer stack. There is no
  secret config file on the NAS.
- The database, your tasks, and your Basecamp login are stored in a Docker volume
  that persists across updates.
- Everything runs from one `docker-compose.yml`: a database plus one app process
  that does the polling, the web UI, and the notifications.

That is it. You now have Basecamp Butler running from 0 to 100.
