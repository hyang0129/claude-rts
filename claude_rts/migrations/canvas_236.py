"""Epic #236 child 5 — canvas JSON reshape migration.

Pre-epic canvas files were authored by the browser via ``PUT /api/canvases/{name}``.
Each card entry carried client-shaped fields (``session_id`` for terminals,
``cardUid``, ``displayName``/``recoveryScript``, no explicit ``card_id``). After
this slice, canvas JSON is server-authored: ``CardRegistry`` is the in-memory
authority, and every ``apply_state_patch`` triggers a write-through. New card
entries always include a ``card_id`` field.

This module implements:
  * ``is_old_schema(data)`` — discriminator: a non-empty cards array where any
    entry lacks ``card_id`` is old-schema. Empty arrays are treated as new
    (already-migrated; harmless to leave alone).
  * ``migrate_file(path)`` — backup-guarded, idempotent-refusing rewrite of one
    canvas file. Raises ``RuntimeError`` if ``{name}.json.pre-236-backup``
    already exists.
  * ``migrate_canvas_dir(canvases_dir)`` — iterate ``*.json`` (skipping
    ``*.pre-236-backup``), migrate each old-schema file, log per-file status.
  * ``check_canvas_dir(canvases_dir)`` — startup probe. Returns the list of
    canvas paths that are old-schema and have no backup sidecar; the server
    refuses to boot if this is non-empty (with a message naming the CLI flag).

Per intent §9 there is no schema-version field — old/new is structural.
"""

from __future__ import annotations

import json
import pathlib
from typing import Iterable

from loguru import logger

# Backup sidecar suffix appended to the full canvas filename (so
# ``main.json`` + suffix == ``main.json.pre-236-backup``). Sidecars are NOT
# canvas files: they live alongside the real ``.json`` files but are skipped
# by ``list_canvases``/``read_canvas`` because their name doesn't match
# ``_CANVAS_NAME_RE``.
BACKUP_SUFFIX = ".pre-236-backup"

# Error message printed by the startup schema check. Names the CLI flag.
STARTUP_ERROR_TEMPLATE = (
    "Canvas file {path} is in pre-epic-#236 schema and has no migration backup. "
    "Run: python -m claude_rts --migrate-canvases"
)


def is_old_schema(data: dict) -> bool:
    """Return True if ``data`` is a pre-epic canvas snapshot.

    Discriminator: the file has a ``cards`` list and at least one entry that
    lacks a ``card_id`` field. New server-authored snapshots always include
    ``card_id`` on every entry. Empty card arrays are treated as new — there's
    nothing to migrate, and the file is structurally compatible with the new
    reader path.
    """
    if not isinstance(data, dict):
        return False
    cards = data.get("cards")
    if not isinstance(cards, list) or not cards:
        return False
    for entry in cards:
        if not isinstance(entry, dict):
            continue
        if "card_id" not in entry:
            return True
    return False


def _translate_card(entry: dict) -> dict:
    """Translate one pre-epic card entry into the new server-snapshot shape.

    The translation is conservative: every key already in ``entry`` is
    preserved, and a stable ``card_id`` is added. For terminals the legacy
    ``session_id`` is reused as the ``card_id`` (the registry shares one key
    space). For widgets and other types without a stable id, ``cardUid`` is
    used if present; otherwise a synthesised id derived from type + index is
    assigned by the caller (see ``_translate_cards``).
    """
    out = dict(entry)
    if "card_id" in out:
        return out
    sid = out.get("session_id")
    if isinstance(sid, str) and sid:
        out["card_id"] = sid
        return out
    cuid = out.get("cardUid")
    if isinstance(cuid, str) and cuid:
        out["card_id"] = cuid
        return out
    # Fallback assigned by caller (needs index + uniqueness scope).
    return out


def _translate_cards(cards: list) -> list:
    """Translate every entry, synthesising ids where necessary."""
    out: list = []
    used_ids: set[str] = set()
    for i, entry in enumerate(cards):
        if not isinstance(entry, dict):
            # Drop unstructured entries — old client never wrote these but
            # be defensive against hand-edits.
            logger.warning("migrate: dropping non-dict card entry at index {}", i)
            continue
        translated = _translate_card(entry)
        if "card_id" not in translated:
            base = translated.get("type", "card")
            synthesised = f"migrated-{base}-{i}"
            # In the unlikely case of a collision, append the index.
            while synthesised in used_ids:
                synthesised = f"{synthesised}-x"
            translated["card_id"] = synthesised
        used_ids.add(translated["card_id"])
        out.append(translated)
    return out


def migrate_file(path: pathlib.Path) -> bool:
    """Migrate one canvas file in place. Idempotent-refusing.

    Returns True if the file was migrated, False if it was already in the
    new schema (no-op). Raises ``RuntimeError`` if a backup sidecar already
    exists — this is the structural guarantee that the migration cannot be
    run twice and silently corrupt user data.
    """
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Canvas file {path} is not valid JSON: {exc}") from exc

    if not is_old_schema(data):
        logger.info("migrate: {} already in new schema, skipping", path.name)
        return False

    if backup.exists():
        raise RuntimeError(
            f"Refusing to migrate {path.name}: backup sidecar {backup.name} already exists. "
            "Either restore from the backup (mv) or delete the backup if you have "
            "intentionally re-converted the canvas."
        )

    # Write backup BEFORE translating — atomic in the failure-mode sense:
    # if the translation crashes, the user has the original on disk.
    backup.write_text(raw, encoding="utf-8")

    new_cards = _translate_cards(data.get("cards", []))
    data["cards"] = new_cards
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info("Migrated: {} -> {}", path.name, backup.name)
    return True


def _candidate_files(canvases_dir: pathlib.Path) -> Iterable[pathlib.Path]:
    """Yield every ``*.json`` in ``canvases_dir`` excluding backup sidecars."""
    if not canvases_dir.exists():
        return
    for p in sorted(canvases_dir.glob("*.json")):
        if p.is_file() and not p.name.endswith(".json" + BACKUP_SUFFIX):
            yield p


def migrate_canvas_dir(canvases_dir: pathlib.Path) -> dict:
    """Migrate every old-schema canvas file in ``canvases_dir``.

    Returns a summary dict::

        {"migrated": [...], "skipped": [...], "errors": [(path, msg), ...]}

    The function does NOT raise if individual files fail — it logs and
    accumulates errors so a partial run can be inspected. The CLI wrapper
    returns a non-zero exit code if any errors were collected.
    """
    summary: dict = {"migrated": [], "skipped": [], "errors": []}
    for path in _candidate_files(canvases_dir):
        try:
            migrated = migrate_file(path)
        except RuntimeError as exc:
            logger.error("migrate: {}", exc)
            summary["errors"].append((str(path), str(exc)))
            continue
        if migrated:
            summary["migrated"].append(str(path))
        else:
            summary["skipped"].append(str(path))
    return summary


def check_canvas_dir(canvases_dir: pathlib.Path) -> list[pathlib.Path]:
    """Return canvas paths that are old-schema AND have no backup sidecar.

    Used by the server startup path: a non-empty return value means the user
    needs to run ``--migrate-canvases`` before the server will boot. Files
    that are old-schema but already have a sidecar are treated as "in the
    middle of a manual recovery" and reported separately by
    ``check_canvas_dir_strict`` — but the default boot check is forgiving
    (it only blocks on files where data could be silently lost).
    """
    blocking: list[pathlib.Path] = []
    for path in _candidate_files(canvases_dir):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Treat unreadable files as someone-else's-problem — let the
            # canvas reader path surface the error at ``GET`` time.
            continue
        if not is_old_schema(data):
            continue
        backup = path.with_name(path.name + BACKUP_SUFFIX)
        if backup.exists():
            # The user already has a sidecar — they probably restored an old
            # backup intentionally. Don't block startup on it; the canvas
            # GET path will simply read the old-shaped JSON and the frontend
            # will display whatever it can.
            continue
        blocking.append(path)
    return blocking
