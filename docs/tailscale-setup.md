# Tailscale Setup Guide — Remote Access

This guide walks you through exposing a `supreme-claudemander` server on your LAN-free mesh via [Tailscale](https://tailscale.com/), so you can open the canvas on a second machine (laptop, desktop at work, a friend's computer) without standing up a VPN or opening ports on your router.

**Scope.** Desktop browser only. Two machines. Personal use. The guide does not cover mobile browsers, multi-user sharing, or public internet exposure via reverse proxy — those are intentionally out of scope.

**What Tailscale gives you.** Every machine you enrol in your tailnet gets a stable `100.x.x.x` address that is reachable from every other machine in the same tailnet, routed over WireGuard. From supreme-claudemander's perspective this looks like a LAN connection: no origin validation surprises, no TLS termination, no reverse proxy.

---

## Prerequisites

- A [Tailscale account](https://login.tailscale.com/start) (free tier is sufficient for personal use).
- **Host machine** — the computer that will run `python -m claude_rts` and hold your Docker containers.
- **Remote machine** — the computer you want to open the canvas on. Must have a desktop browser (Chrome, Edge, Firefox, Safari).
- Both machines must be able to reach the public internet long enough to complete Tailscale enrolment. After enrolment they communicate peer-to-peer over the tailnet.

---

## Step 1 — Install Tailscale on the host machine

Pick the instructions that match your host OS.

**Linux** (Debian, Ubuntu, Fedora, Arch, etc.)
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```
`tailscale up` prints a URL. Open it in any browser and sign in to your Tailscale account to enrol the machine.

**macOS**
1. Install the Tailscale app from the [Mac App Store](https://apps.apple.com/us/app/tailscale/id1475387142) or [download it directly](https://tailscale.com/download/mac).
2. Open the app, click the menu-bar icon, and sign in.

**Windows**
1. Download the installer from [tailscale.com/download/windows](https://tailscale.com/download/windows).
2. Run it, then sign in through the tray icon.

Verify the host is enrolled:
```bash
tailscale status
```
You should see the host listed with a `100.x.x.x` address.

---

## Step 2 — Install Tailscale on the remote machine

Repeat Step 1 on the remote machine, signing in to the **same** Tailscale account.

When both machines are signed in to the same account, they are automatically in the same tailnet — no invite, no ACL edits, no key exchange needed.

Verify from the remote machine:
```bash
tailscale status
```
You should see both machines listed.

---

## Step 3 — Find the host machine's Tailscale IP

On the **host** machine:
```bash
tailscale ip -4
```
This prints a single line like `100.64.12.34`. Write it down — the remote machine will use this address to reach the server.

---

## Step 4 — Start the server bound to all interfaces

By default, `python -m claude_rts` binds to `127.0.0.1` and is only reachable from the host itself. To make it reachable over the tailnet, pass `--host 0.0.0.0`:

```bash
python -m claude_rts --host 0.0.0.0
```

The server will now accept connections on every interface — including the Tailscale interface. The startup log line will read:
```
supreme-claudemander starting on http://0.0.0.0:3000
```

Leave the server running. Press `Ctrl+C` to stop it when you are done.

> **Remote access is opt-in.** Running `python -m claude_rts` with no flags still binds to `127.0.0.1` and is not reachable from other machines. You must pass `--host 0.0.0.0` every time you want remote access.

---

## Step 5 — Open the canvas from the remote machine

On the **remote** machine, open a desktop browser and navigate to:
```
http://<host-tailscale-ip>:3000
```

Using the example from Step 3: `http://100.64.12.34:3000`.

The canvas should load exactly as it does on the host. Pan, zoom, spawn terminal cards, and interact with your containers as normal.

---

## Step 6 — Test the connection (optional)

If the browser can't reach the canvas, confirm the tailnet path is working before debugging the app:

```bash
# On the remote machine
curl http://<host-tailscale-ip>:3000/api/hubs
```

A successful response is a JSON array (possibly empty) — for example `[]` or `[{"name": "..."}]`. If `curl` hangs or reports "Connection refused":

- Verify `tailscale status` on both machines shows the other as `active`.
- Verify the server is still running on the host (`python -m claude_rts --host 0.0.0.0`) and that the startup log shows `http://0.0.0.0:3000`.
- Check host firewall: Linux `ufw`/`firewalld`, macOS firewall in System Settings, Windows Defender Firewall. Allow inbound TCP on port 3000 from the Tailscale interface.

---

## Access control

**Tailscale is the auth boundary.** supreme-claudemander has no login layer, no HTTP Basic Auth, no session tokens. If a device is in your tailnet it can reach the canvas; if it is not, it cannot. This is a deliberate design decision — the project targets personal use and delegates access control to Tailscale's ACL system.

If you want to restrict which devices in your tailnet can reach the server (for example, a shared machine that should not have canvas access), edit your [Tailscale ACL](https://tailscale.com/kb/1018/acls) to deny traffic to port 3000 from that device. Do **not** add application-level authentication on top — if you need that, file an issue first.

---

## Kill criterion and maintenance note

This guide relies on Tailscale remaining viable for personal mesh use. **If by 2027-01-01 Tailscale becomes paid-only for personal mesh use, or if WebSocket latency from your setup consistently exceeds 500ms, this guide may need revision.** At that point, evaluate alternatives — this guide does not pre-document fallbacks (WireGuard, Cloudflare Tunnel, ngrok) because an untested fallback creates false confidence.

If you hit either of those conditions, open an issue so the project can re-evaluate the remote-access story.

---

## For repository owners: CI setup

The [Tailscale CI job](../.github/workflows/) (child issue #226 of epic #119) authenticates runners against your tailnet using a Tailscale [OAuth client](https://tailscale.com/kb/1215/oauth-clients). If you want to enable that job on a fork or clone of this repo, you need to create two GitHub Actions secrets in your repository settings:

1. Visit [login.tailscale.com/admin/settings/oauth](https://login.tailscale.com/admin/settings/oauth) and create an OAuth client with the `auth_keys` scope. Copy the client ID and secret.
2. In GitHub: **Settings → Secrets and variables → Actions → New repository secret**. Add:
   - `TS_OAUTH_CLIENT_ID` — the OAuth client ID from step 1.
   - `TS_OAUTH_SECRET` — the OAuth client secret from step 1.

The Tailscale GitHub Action (`tailscale/github-action@v2`) will use these to bring the runner into your tailnet for the duration of the job. No other configuration is required on the repo side.

End users running the server locally do **not** need these secrets — they only matter if you are running the automated CI job that verifies remote access.
