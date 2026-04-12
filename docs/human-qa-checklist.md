# Human QA Checklist

Run this after any significant change to verify end-to-end behaviour that automated tests cannot cover.

## Launch

```bash
python -m claude_rts --electron --dev-config human-qa
```

The `human-qa` preset opens with four cards pre-placed:
- **Terminal** (top-left) — canvas nav, drag/resize, Start Claude button
- **VM Manager** (top-right) — container lifecycle and actions
- **Profile Manager** (bottom-left) — profile selection and priority
- **Canvas Claude** (bottom-right) — MCP connection, canvas tool use

Priority profile: `hongy2`

---

## 1. Canvas Navigation

- [ ] **Pan** — middle-click-drag (or space+drag) moves the canvas; cards stay in relative position
- [ ] **Zoom in** — Ctrl+scroll up (or `+` key) increases zoom; cards scale up
- [ ] **Zoom out** — Ctrl+scroll down (or `-` key) decreases zoom; cards scale down
- [ ] **Zoom reset** — double-click canvas background resets to 100% / origin
- [ ] **Cards stay in bounds** — after zoom, no card clips through the canvas edge

---

## 2. Card Drag & Resize

Use the pre-placed Terminal card.

- [ ] **Drag** — click-drag the card header; card moves to new position, snapping to grid
- [ ] **Resize** — drag the resize handle (bottom-right corner); card grows/shrinks
- [ ] **Min size enforced** — resize below minimum; card stops at minimum, not smaller
- [ ] **Z-order** — click a card behind another; it comes to the front
- [ ] **Position persists** — save canvas (see §8), reload; card is at the new position

---

## 3. Terminal Card

- [ ] **Shell prompt** — Terminal card connects to the util container and shows a bash prompt within ~5 s
- [ ] **Keystrokes** — type `echo hello`; output `hello` appears on the next line
- [ ] **Scrollback** — run `seq 1 100`; scroll up to see earlier output
- [ ] **Resize reflow** — resize the card; terminal reflows to new cols/rows (no garbled output)
- [ ] **Session persistence** — close the card via the × button; spawn a new terminal; run `ls` — new session starts fresh (no stale scrollback from prior session)

---

## 4. Start Claude Button

Requires `hongy2` to be the priority profile (already set in the preset).

- [ ] **Button visible** — Terminal card header shows the Claude icon button
- [ ] **Correct command** — click the button; terminal runs `env CLAUDE_CONFIG_DIR=/profiles/hongy2 claude`
- [ ] **Claude launches** — Claude's startup screen (ASCII art + `/mcp` panel) appears within ~10 s
- [ ] **No trust dialog** — no "Yes, I trust this folder" prompt appears at any point
- [ ] **No priority → warning** — open Profile Manager, clear the priority profile, click the button again; terminal shows "No priority profile set" warning (not an error crash)

---

## 5. VM Manager

Pre-configured favorites: `supreme-claudemander-util` (online), `does-not-exist` (missing).

### Discovery
- [ ] **Online container** — `supreme-claudemander-util` shows a green indicator and a **Stop** button
- [ ] **Missing container** — `does-not-exist` shows a "not found" label with **no** Start button (only a Remove button)
- [ ] **Refresh** — stop a running container externally (`docker stop <name>`), wait, click Refresh; status updates to offline and Start button appears

### Favorites management
- [ ] **Search** — type a partial container name in the search box; results filter live, exclude existing favorites
- [ ] **Add favorite** — click + on a search result; it appears in the favorites list
- [ ] **Remove favorite** — click ✕ on a favorite; it is removed from the list
- [ ] **Persistence** — add a favorite, save canvas (§8), reload; favorite is still present

### Container lifecycle
- [ ] **Stop** — click Stop on `supreme-claudemander-util`; status changes to offline within ~5 s and Start button appears
- [ ] **Start** — click Start on the now-offline util; status returns to online within ~10 s

### Actions
- [ ] **Terminal action** — click the Terminal action button on `supreme-claudemander-util`; a new terminal card opens connected to that container
- [ ] **Claude action** — click the Claude action button; terminal opens and runs the claude command with the priority profile
- [ ] **Disabled when offline** — stop the container; action buttons dim and are non-interactive

### Configure actions
- [ ] **Open dialog** — click ⚙ on a favorite; JSON editor opens with the current actions array
- [ ] **Save change** — edit a label, click Save; the button label updates in the card
- [ ] **Invalid JSON rejected** — enter malformed JSON, click Save; error message shown, dialog stays open
- [ ] **Cancel** — click Cancel; no change applied

---

## 6. Profile Manager

- [ ] **Profiles listed** — Profile Manager shows `hongy2` and any other profiles from `~/.claude-profiles/`
- [ ] **Priority highlighted** — `hongy2` has a visual priority indicator (star or bold)
- [ ] **Change priority** — click another profile to set it as priority; indicator moves; `/api/profiles/priority` returns the new name
- [ ] **Burn rate / usage** — profiles with usage data show approximate token burn rate
- [ ] **No crash on empty** — profiles with no usage data display cleanly (no JS error in console)

---

## 7. Canvas Claude — MCP Connection (issue #114)

- [ ] **MCP connected** — within ~15 s of the card becoming interactive, type `/mcp` in the Claude TUI; output shows `canvas · ✓ connected`
- [ ] **All 9 tools listed** — `/tools` or tool listing shows `vm_discover_containers`, `open_terminal`, and the other 7 canvas tools
- [ ] **No trust dialog** — no "Yes, I trust this folder" prompt during startup
- [ ] **Tool round-trip** — ask Claude to call `vm_discover_containers`; it returns the real container list from the canvas server
- [ ] **Reattach stable** — close the Canvas Claude card, reopen it (re-attaches to the live tmux session); `/mcp` still shows `canvas · ✓ connected`

---

## 8. Canvas Persistence

- [ ] **Save** — rearrange a card, open the save dialog (Ctrl+S or toolbar), save as `human-qa-test`; success confirmation shown
- [ ] **Reload** — kill and relaunch the server with `--dev-config human-qa`; open the saved canvas; card positions match what was saved
- [ ] **Default canvas loads** — on fresh launch, the `human-qa` canvas loads automatically (no manual selection needed)

---

## 9. Regression Smoke

Run after any of the above sections reveal a bug and a fix is applied.

- [ ] Spawn 3 terminal cards; all connect independently with separate sessions
- [ ] Pan and zoom while a terminal is receiving output; no visual corruption
- [ ] Open VM Manager search; type rapidly; no race condition / blank results
- [ ] Reload the page (F5 in Electron); canvas reloads from last saved state without JS errors in DevTools console

---

## Sign-off

| Area | Pass | Notes |
|---|---|---|
| Canvas navigation | | |
| Drag & resize | | |
| Terminal card | | |
| Start Claude button | | |
| VM Manager | | |
| Profile Manager | | |
| Canvas Claude MCP | | |
| Canvas persistence | | |
| Regression smoke | | |

**Tester:** _______________  **Date:** _______________  **Build:** _______________
