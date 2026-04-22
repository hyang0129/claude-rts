# State Model — server-owned vs per-device

**Status:** load-bearing. This document is a code-level invariant, not a convention. Any PR that introduces authoritative state on the client — or that writes card state through any path other than the single mutation endpoint below — is rejected in review.

**Related rule:** see the "State ownership" section of the repo root [`CLAUDE.md`](../CLAUDE.md).

## Mental model

The canvas is a "fancy tmux" / "RuneScape login" — the server holds the one true state of every card (position, size, z-order, display name, recovery script, starred, existence, which canvas it belongs to). Clients are render surfaces. When a user drags a card on their laptop, the server learns the new position and every other attached client renders the same new position. The client never holds authoritative state that another client could disagree with.

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

This list is not exhaustive of future card fields. **Any new field added to a card is server-owned by default.** A new field is client-owned only if it is explicitly added to the per-device allowlist below, and adding to the allowlist requires a reviewer to agree it meets the allowlist criterion (render-only, no multi-client consistency requirement).

## Per-device (client-owned) allowlist

These fields are rendered locally, are not synchronised across clients, and are not expected to be consistent between a laptop and a desktop attached to the same session:

- `pan.x` / `pan.y` — the viewport translation
- `zoom` — the viewport scale factor
- Focus — which card or element the user has focused on their local screen
- `canvasMode` — the local UI mode for the canvas (if present)
- Minimap toggle — whether the minimap overlay is shown locally
- `controlGroups` — the user's locally-bound card shortcut groups

**Adding a field to this allowlist is a deliberate decision.** The criterion is: the field is render-only and has no multi-client consistency requirement. If a reviewer cannot defend the field on both counts, the field is server-owned.

## Single mutation path

All server-owned mutations flow through:

```
PUT /api/cards/{id}/state
```

This endpoint is specified by epic #236 child 2 (issue [#238](https://github.com/hyang0129/supreme-claudemander/issues/238)). Until child 2 merges, the endpoint is the *intended* mutation path — it is the path every current and future card-state mutation is being migrated onto. No new code path may be added that writes card state outside this endpoint.

Forbidden patterns:

- Client code that updates `card.x` / `card.y` / `card.starred` / etc. in its own state and declares that the authoritative value.
- Card-type-specific REST endpoints that mutate a server-owned field without going through `PUT /api/cards/{id}/state` under the hood.
- Write-through from the browser directly to a canvas JSON file.
- Any in-memory cache on the client that a later read prefers over the server's value.

The only valid write flow is: user action → `PUT /api/cards/{id}/state` → server updates authoritative state → server broadcasts the update → every attached client (including the originating one) re-renders from the broadcast.

## Code-review checklist

When reviewing any PR that touches card fields, card rendering, or state wiring, walk this checklist:

- [ ] Does the PR add a new field to a card? If yes, is the field server-owned (default), or added to the per-device allowlist above with an explicit justification?
- [ ] Does the PR introduce any client-side assignment of the form `this.<field> = ...` or `card.<field> = ...` for a server-owned field, followed by reads that treat the local value as authoritative? If yes, reject — all mutations must round-trip through `PUT /api/cards/{id}/state`.
- [ ] Does the PR add a new REST endpoint that mutates a server-owned field? If yes, reject unless the endpoint is layered on top of the single mutation path (or the PR is the one that *is* migrating a legacy endpoint).
- [ ] Does the PR add a cache, local storage entry, or IndexedDB slot for a server-owned field? If yes, reject — the server is the cache.
- [ ] Does the PR write directly to a canvas JSON file from the browser, or from a server path that bypasses the single mutation endpoint? If yes, reject.
- [ ] If the PR adds a per-device field, does it update this document's allowlist? If no, reject until the allowlist is updated.

## Counter-example

A PR adds `this.pinned = true` on a terminal card on the client, reads `this.pinned` locally to decide rendering, and never round-trips through the server. Reject: `pinned` is not on the per-device allowlist, so it is server-owned by default; an authoritative client value for a server-owned field is the exact pattern the CLAUDE.md rule prohibits. The fix is to add `pinned` to the card's server-side schema, mutate it through `PUT /api/cards/{id}/state`, and let the broadcast drive rendering.
