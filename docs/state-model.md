# State Model — server-owned vs per-device

**Status:** load-bearing. This document is a code-level invariant, not a convention. Any PR that introduces authoritative state on the client — or that writes card state through any path other than the single mutation endpoint below — is rejected in review.

**Related rule:** see the "State ownership" section of the repo root [`CLAUDE.md`](../CLAUDE.md).

## Mental model

The canvas is a "fancy tmux" / "RuneScape login" — the server holds the one true state of every card (existence, position, size, z-order, display name, recovery script, starred, which canvas it belongs to). Clients are render surfaces. When a user drags a card on their laptop, the server learns the new position and every other attached client renders the same new position. The client never holds authoritative state that another client could disagree with.

**Existence is server-owned, not just fields.** The server hydrates cards into `CardRegistry` at startup from canvas JSON snapshots. Cards exist in the registry independent of any client connection. MCP tools, monitoring, a second client, and the first browser to attach all see the same set of cards. This is the model commitment from epic #254 — the server is an authoritative process host, not a field-populator that follows the client.

The only state a client legitimately owns is state that *cannot* disagree across clients because it is strictly a property of the local viewport — where the user has panned, how far they have zoomed, which card they have focused on their own screen. A laptop and a desktop can show different pan offsets at the same time without anything being "wrong"; they cannot show different positions for the same card without something being wrong.

## Server-owned fields (authoritative on the server)

Every field in this list is stored on the server, mutated through the single mutation path below, and broadcast to all attached clients. The client renders what the server says; the client does not cache an alternative value.

- Card existence (the card is or is not in the registry)
- `starred`
- `x`
- `y`
- `w`
- `h`
- `z_order`
- `display_name`
- `recovery_script`
- Canvas membership (which canvas the card belongs to)
- `error_state` (server-computed retry/recovery metadata; broadcast but **not persisted** — see "Retry semantics")

This list is not exhaustive of future card fields. **Any new field added to a card is server-owned by default.** A new field is client-owned only if it is explicitly added to the per-device allowlist below, and adding to the allowlist requires a reviewer to agree it meets the allowlist criterion (render-only, no multi-client consistency requirement).

## Server-authored spawning (the boot path)

Cards come into existence on the server, not the client. The full lifecycle:

1. **Boot hydration.** On `on_startup`, the server runs `hydrate_canvas_into_registry(canvas_name)` for each canvas selected by the active policy (see "Canvas-switch hydration policy" below). For each entry in the canvas JSON, the matching card subclass's `from_descriptor(data)` builds the card with full server-owned state (`x`, `y`, `w`, `h`, `starred`, `display_name`, `recovery_script`, etc.) and `CardRegistry.register` installs it. PTY-bearing cards (`TerminalCard`) launch their session in a background task with a bounded retry envelope (see "Retry semantics"). No client connection is required for any of this.

2. **Client render.** When a browser connects, it calls `GET /api/cards?canvas=<name>` and renders the descriptors the server returns. The client never reads canvas JSON files directly, never invents authoritative defaults, and never decides what cards exist. `renderFromDescriptor` is a pure function of the server's response.

3. **User-initiated spawn.** A new card is created via the card-type-specific creation endpoint (e.g. `POST /api/claude/terminal/create`, `POST /api/widgets`, `POST /api/canvas-claude/create`, `POST /api/blueprints/spawn`). The endpoint registers the card in `CardRegistry` and broadcasts `card_created`; every attached client renders the new card from the broadcast.

4. **Mutation.** All server-owned mutations flow through `PUT /api/cards/{id}/state` (see "Single mutation path" below).

5. **Reconnect / second-client attach.** Browsers reconnecting or attaching for the first time call `GET /api/cards?canvas=X` and render whatever the server holds. There is no "first attacher wins" — the server's state is the truth, and any client renders it identically.

### `/ws/session/new` is user-initiated only

Post-#254, `/ws/session/new` (and equivalent attach endpoints for other PTY-bearing card types) are reserved for user-initiated terminal spawn. Boot, canvas switch, and reconnect flows **never** trigger them. The pre-#254 "client opens a WebSocket per canvas entry to construct the server-side card" pattern is gone; reintroducing it is a DP-1 regression.

When a user creates a new terminal interactively, the create endpoint registers the card first, then opens the WebSocket to attach to the now-existing PTY. The WebSocket never decides whether the card exists.

### Forbidden patterns at spawn time

- A client-side `spawnFromSerialized` that reads canvas JSON and opens WebSockets per entry. This is the pattern #254 eliminated.
- A client-side default value for a server-owned field at spawn time (`starred: true` fallback in `handleControlCardCreated`, etc.). The server's `from_descriptor` is the only source of defaults.
- Any code path that creates a card on the server as a side-effect of a client connection at boot. The server is not a field-populator; it is an authoritative process host.

## Canvas-switch hydration policy

Two policies are supported, controlled by `canvas_switch_policy` in `config.json`. Both are server-authored — neither has the client driving registration.

- **`keep_resident`** (default): every canvas on disk is hydrated at startup. All canvases are observable via `GET /api/cards?canvas=<any>` from the moment the server is up. Switching between canvases is a pure render change on the client; the server holds every canvas resident. PTYs for non-active canvases keep running. Memory pressure is the user's to manage.

- **`lazy_hydrate`**: only the default canvas is hydrated at startup. The first switch into another canvas triggers `POST /api/canvases/{name}/activate`, which runs `hydrate_canvas_into_registry` for that canvas. Activate is idempotent — once a canvas is resident, subsequent activates are no-ops. Choose this policy if you have many canvases and only switch into a small subset per session.

The decision to default to `keep_resident` was made under epic #254 child 4 (#259): the "server-as-tmux-remote" model is more honest under keep-resident — every long-running PTY survives canvas switching the same way every tmux session survives a detach — and the cost is bounded by the canvases the user actually creates.

### `/api/canvases/{name}/activate` semantics

```
POST /api/canvases/{name}/activate
→ {"name": <canvas>, "policy": <policy>, "hydrated": <int>, "already_resident": <bool>}
```

- 404 if the canvas file does not exist.
- `already_resident: true` if any card on the canvas is already in `CardRegistry` (idempotent fast path).
- Under `keep_resident`, the canvas was hydrated at boot, so this is always a fast no-op.
- Under `lazy_hydrate`, this is the *only* path that hydrates non-default canvases.

The client calls this before `GET /api/cards?canvas=X` on canvas switch so the server-authored model holds even under lazy hydration.

## Retry semantics

`TerminalCard.start()` wraps `SessionManager.create_session` in a bounded jittered retry. Default retry intervals are `[10s, 30s, 90s]` (max 3 attempts) to tolerate containers that are still coming up.

- On success at any attempt, the PTY attaches and `error_state` is `None`.
- On exhaustion of all retries, `error_state` is set to `{kind: "container_unavailable", attempts: N, last_error: "..."}` and a `card_updated` broadcast fires so attached clients can render a "unable to connect to container" UI with a manual retry button.
- The manual retry button calls the create-or-attach endpoint, which clears `error_state` and reruns the retry envelope.
- `error_state` is **server-owned** but **not persisted** to the canvas snapshot — retries reset to `0` on every server restart. This is recovery metadata, not state. The persist callback strips `error_state` before writing.

## Hydrate-as-starred even when the container is gone

If a snapshot names a container that has since been `docker rm`'d, the card is hydrated as starred anyway. We do not silently drop starred cards. The user investigates: the card surfaces in `error_state`, the manual retry button is exposed, and the user decides to either restore the container or unstar the card. This is deliberate — silently dropping starred cards would erase user intent without telling them.

## Per-device (client-owned) allowlist

These fields are rendered locally, are not synchronised across clients, and are not expected to be consistent between a laptop and a desktop attached to the same session:

- `pan.x` / `pan.y` — the viewport translation
- `zoom` — the viewport scale factor
- Focus — which card or element the user has focused on their local screen
- `canvasMode` — the local UI mode for the canvas (if present)
- Minimap toggle — whether the minimap overlay is shown locally
- `controlGroups` — the user's locally-bound card shortcut groups

**The per-device allowlist is a first-class mechanism, not a last resort.** It exists precisely so genuinely device-local state can live on the client without violating DP-1. A reviewer who reflexively rejects "any client-owned state at all" is misreading this document. The criterion is: the field is render-only and has no multi-client consistency requirement. If a reviewer cannot defend the field on both counts, the field is server-owned. If a reviewer *can* defend the field on both counts, the field belongs on the allowlist and is added by updating this document.

### Worked example — terminal scrollback search cursor position

Suppose we want to add Ctrl-F search to terminal cards. The user types a query, the terminal highlights matches in its scrollback, and pressing F3 advances to the next match. We need to remember "which match index the user is currently focused on" between keystrokes. Call this field `searchCursor`.

**Step 1 — is this server state?** Walk the criterion:

- *Render-only?* Yes. `searchCursor` only affects which scrollback line is highlighted on the local screen. It does not change the PTY, the scrollback contents, the card position, or anything any other client would see.
- *Multi-client consistency requirement?* No. If the user has the same card open on a laptop and a desktop, "match #4" on the laptop and "match #2" on the desktop is fine — they are independently navigating the same scrollback on different screens. Forcing them to share a cursor would be a bug, not a feature.

Both criteria are met. `searchCursor` belongs on the per-device allowlist.

**Step 2 — would server-owning it be wrong?** Yes, in two ways. (a) Every F3 keystroke would round-trip through `PUT /api/cards/{id}/state` and broadcast to every attached client, wasting bandwidth on a value no other client cares about. (b) The desktop's cursor would jump every time the laptop pressed F3, creating cross-device flicker for state that has no consistency requirement. This is the "shoehorn device-local state into server state" failure mode the allowlist exists to prevent.

**Step 3 — would localStorage-without-allowlist be wrong?** Yes. Hiding device-local state in `localStorage` without documenting it is exactly the failure mode the allowlist criterion catches. A future reader inspecting "what state does this client own?" needs the answer to be in this document, not implicit in browser-storage code.

**Step 4 — adding it to the allowlist.** Edit the bulleted list above to include:

> - `searchCursor` — the user's current scrollback-search match index per terminal card

Then implement `searchCursor` as a local field on the client `TerminalCard` instance, persisted to `localStorage` keyed by `card_id` if survival across page reload is wanted. Do **not** add it to `from_descriptor`, do **not** add it to `to_descriptor`, do **not** add it to `MUTABLE_FIELDS`, and do **not** broadcast it.

**Step 5 — what review still rejects.** The allowlist addition does not legalise:

- Storing the *card's* `x` / `y` / `starred` / `display_name` in `localStorage`. Those are server-owned; the allowlist criterion fails on consistency.
- Letting the client invent a default for `searchCursor` and then using that default to override a server value. There is no server value; the client owns the field outright.
- Making `searchCursor` a GET parameter on `/api/cards` so the server caches it. The whole point is the server doesn't know.

A PR that adds `searchCursor` to the allowlist with this justification, implements it client-only, and updates this document is approved. A PR that adds it without updating this document is rejected.

## Single mutation path

All server-owned mutations flow through:

```
PUT /api/cards/{id}/state
```

This endpoint is the single disk-and-memory write path for every server-owned field. Card-type-specific aliases (e.g. `PUT /api/claude/terminal/{id}/rename`, `PUT /api/claude/terminal/{id}/recovery-script`) all funnel through `CardRegistry.apply_state_patch` under the hood — they are surface-area conveniences, not separate authoritative paths.

Forbidden patterns:

- Client code that updates `card.x` / `card.y` / `card.starred` / etc. in its own state and declares that the authoritative value.
- Card-type-specific REST endpoints that mutate a server-owned field without going through `apply_state_patch` under the hood.
- Write-through from the browser directly to a canvas JSON file.
- Any in-memory cache on the client that a later read prefers over the server's value.

The only valid write flow is: user action → `PUT /api/cards/{id}/state` → server updates authoritative state → server broadcasts the update → every attached client (including the originating one) re-renders from the broadcast.

## BlueprintCard ephemerality

`BlueprintCard` is the one card type that **does not** participate in server-authored hydration, and that is intentional.

- `BlueprintCard.serialize()` returns `None`. The persist callback skips it. Live BlueprintCards never appear in the canvas JSON snapshot.
- `BlueprintCard.from_descriptor(data)` raises `NotImplementedError`. There is no canvas-snapshot entry for a BlueprintCard to be hydrated from in the first place.
- `BlueprintCard` self-unregisters on completion or failure. It is one-shot by design — a server-side orchestrator that runs a step list and exits.

Treat BlueprintCard as the deliberate counter-example to "all card types migrate together." Persistent card types (Terminal, Widget, CanvasClaude) hydrate; BlueprintCard does not. A regression test (`tests/test_blueprint_card.py::test_blueprint_card_excluded_from_canvas_snapshot`) asserts a live BlueprintCard never appears in the canvas JSON snapshot.

The blueprint *definition* (a YAML/JSON document under `~/.supreme-claudemander/blueprints/`) is persisted; the running BlueprintCard *instance* is not. To re-run a blueprint after server restart, the user spawns it again via `POST /api/blueprints/spawn`.

### Resolved open questions

- **U1 (canvas switch — unload or keep-resident?)** — Resolved under #259. Default policy is `keep_resident`; `lazy_hydrate` is opt-in via `config.canvas_switch_policy`. See "Canvas-switch hydration policy" above.
- **U2 (BlueprintCard 'canvas' provenance — server-side or client-supplied?)** — Resolved as **defer**. BlueprintCard's `context` (including any `canvas` provenance) remains client-user-supplied for now. Moving substitution server-side would require a non-trivial rewrite of the blueprint context model and is out of scope for #254. If a future feature genuinely depends on server-side canvas-provenance resolution, open a follow-up epic; do not bolt it onto the existing one-shot BlueprintCard lifecycle.
- **U3 (`/ws/session/new` restriction mechanism)** — Resolved under #258 / #271. The handler now validates that the matching `card_uid` exists in `CardRegistry` and refuses to create a new card if it doesn't. Boot and switchCanvas paths render from `GET /api/cards?canvas=X` and never call `/ws/session/new` with courier params.

## Code-review checklist

When reviewing any PR that touches card fields, card rendering, or state wiring, walk this checklist:

- [ ] Does the PR add a new field to a card? If yes, is the field server-owned (default), or added to the per-device allowlist above with an explicit justification? **The allowlist is a legitimate destination, not a failure** — see the worked example above. Reject only if the justification fails the render-only / no-consistency-requirement criterion.
- [ ] Does the PR introduce any client-side assignment of the form `this.<field> = ...` or `card.<field> = ...` for a server-owned field, followed by reads that treat the local value as authoritative? If yes, reject — all mutations must round-trip through `PUT /api/cards/{id}/state`.
- [ ] Does the PR add a new REST endpoint that mutates a server-owned field? If yes, reject unless the endpoint is layered on top of the single mutation path (or the PR is the one that *is* migrating a legacy endpoint).
- [ ] Does the PR add a cache, local storage entry, or IndexedDB slot for a *server-owned* field? If yes, reject. (Local storage for a *per-device-allowlist* field is fine — the allowlist exists for this case.)
- [ ] Does the PR write directly to a canvas JSON file from the browser, or from a server path that bypasses the single mutation endpoint? If yes, reject.
- [ ] If the PR adds a per-device field, does it update this document's allowlist with a justification? If no, reject until the allowlist is updated.
- [ ] Does the PR introduce a new card type that participates in canvas snapshots? If yes, does it implement `from_descriptor` so server-authored hydration works? Reject any persistent card type that cannot be hydrated server-side without a client connection. (BlueprintCard is the documented exception — see above.)
- [ ] Does the PR add a code path that creates a server-side card as a side-effect of a client connection at boot or canvas switch? If yes, reject — this is the pre-#254 pattern.

## Counter-examples

### `pinned` as a misclassified field

A PR adds `this.pinned = true` on a terminal card on the client, reads `this.pinned` locally to decide rendering, and never round-trips through the server. Reject: `pinned` is a multi-user-meaningful concept (the user wants this card to stand out across all their devices), so it fails the consistency-requirement leg of the allowlist criterion. It is server-owned by default; an authoritative client value for a server-owned field is the exact pattern the CLAUDE.md rule prohibits. The fix is to add `pinned` to the card's server-side schema, mutate it through `PUT /api/cards/{id}/state`, and let the broadcast drive rendering.

### `searchCursor` as a correctly-classified field

The same author opens a separate PR adding `searchCursor` (the example above), updates this document's allowlist to include it, implements it client-only with a `localStorage` shim, and does not touch any server endpoint. Approve: the field passes both legs of the allowlist criterion (render-only, no consistency requirement), and the document update means a future reviewer can answer "what client-owned state does this card carry?" by reading this file.

The two PRs differ not in code complexity but in *which side of the boundary the field belongs on*. The allowlist criterion is the boundary.
