"""Tests for the epic #236 child 5 canvas JSON migration.

Covers:
  * ``is_old_schema`` discriminator behaviour.
  * ``migrate_file`` happy path: backup + rewrite + idempotent-refusing.
  * ``migrate_canvas_dir`` summary across mixed input.
  * ``check_canvas_dir`` startup probe (returns blocking files only).
"""

import json
import pathlib

import pytest

from claude_rts.migrations import canvas_236


# ── Discriminator ───────────────────────────────────────────


def test_is_old_schema_true_for_legacy_terminal_entry():
    data = {
        "name": "main",
        "cards": [
            {"type": "terminal", "session_id": "abc", "hub": "h1", "x": 0, "y": 0, "w": 200, "h": 100},
        ],
    }
    assert canvas_236.is_old_schema(data) is True


def test_is_old_schema_false_when_card_id_present():
    data = {
        "name": "main",
        "cards": [
            {"type": "terminal", "card_id": "abc", "session_id": "abc", "hub": "h1"},
        ],
    }
    assert canvas_236.is_old_schema(data) is False


def test_is_old_schema_false_for_empty_cards():
    """An empty cards array is treated as new — nothing to migrate."""
    assert canvas_236.is_old_schema({"name": "x", "cards": []}) is False


def test_is_old_schema_false_for_non_dict_input():
    assert canvas_236.is_old_schema([]) is False
    assert canvas_236.is_old_schema(None) is False  # type: ignore[arg-type]


def test_is_old_schema_true_when_any_entry_lacks_card_id():
    """One pre-epic entry is enough to flag the file."""
    data = {
        "cards": [
            {"type": "terminal", "card_id": "ok"},
            {"type": "terminal", "session_id": "old"},  # missing card_id
        ],
    }
    assert canvas_236.is_old_schema(data) is True


# ── migrate_file ────────────────────────────────────────────


def _write(path: pathlib.Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def test_migrate_file_writes_backup_and_translates(tmp_path: pathlib.Path):
    src = tmp_path / "main.json"
    _write(
        src,
        {
            "name": "main",
            "canvas_size": [3840, 2160],
            "cards": [
                {"type": "terminal", "session_id": "sid-1", "hub": "h", "x": 10, "y": 20},
                {"type": "widget", "widgetType": "system-info", "x": 100, "y": 100, "cardUid": "u-1"},
            ],
        },
    )

    migrated = canvas_236.migrate_file(src)
    assert migrated is True

    backup = src.with_name("main.json.pre-236-backup")
    assert backup.exists()
    # Backup is verbatim copy of original.
    assert "card_id" not in backup.read_text()

    new_data = json.loads(src.read_text())
    assert all("card_id" in c for c in new_data["cards"])
    # Discriminator: terminal carries session_id-derived card_id; widget carries cardUid.
    assert new_data["cards"][0]["card_id"] == "sid-1"
    assert new_data["cards"][1]["card_id"] == "u-1"


def test_migrate_file_skips_new_schema(tmp_path: pathlib.Path):
    src = tmp_path / "fresh.json"
    _write(src, {"name": "fresh", "cards": [{"type": "terminal", "card_id": "abc"}]})

    migrated = canvas_236.migrate_file(src)
    assert migrated is False
    # No backup written for a no-op.
    assert not src.with_name("fresh.json.pre-236-backup").exists()


def test_migrate_file_refuses_when_backup_exists(tmp_path: pathlib.Path):
    src = tmp_path / "main.json"
    _write(src, {"name": "main", "cards": [{"type": "terminal", "session_id": "sid"}]})
    backup = src.with_name("main.json.pre-236-backup")
    backup.write_text("{}", encoding="utf-8")

    with pytest.raises(RuntimeError, match="backup sidecar"):
        canvas_236.migrate_file(src)
    # File is not modified by the failed run.
    assert "card_id" not in src.read_text()


def test_migrate_file_synthesises_id_when_no_session_or_uid(tmp_path: pathlib.Path):
    src = tmp_path / "main.json"
    _write(
        src,
        {"name": "main", "cards": [{"type": "loader"}, {"type": "loader"}]},
    )
    canvas_236.migrate_file(src)
    new_data = json.loads(src.read_text())
    ids = [c["card_id"] for c in new_data["cards"]]
    assert ids == ["migrated-loader-0", "migrated-loader-1"]


def test_migrate_file_invalid_json_raises(tmp_path: pathlib.Path):
    src = tmp_path / "broken.json"
    src.write_text("not json{", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not valid JSON"):
        canvas_236.migrate_file(src)


# ── migrate_canvas_dir ──────────────────────────────────────


def test_migrate_canvas_dir_mixed(tmp_path: pathlib.Path):
    canvases = tmp_path / "canvases"
    canvases.mkdir()
    # Old schema → migrated
    _write(canvases / "old.json", {"name": "old", "cards": [{"type": "terminal", "session_id": "x"}]})
    # New schema → skipped
    _write(canvases / "new.json", {"name": "new", "cards": [{"type": "terminal", "card_id": "x"}]})
    # Old schema with backup already → errored
    _write(canvases / "blocked.json", {"name": "blocked", "cards": [{"type": "terminal", "session_id": "y"}]})
    (canvases / "blocked.json.pre-236-backup").write_text("{}", encoding="utf-8")
    # Backup-only file: ignored entirely.
    (canvases / "stray.json.pre-236-backup").write_text("{}", encoding="utf-8")

    summary = canvas_236.migrate_canvas_dir(canvases)
    assert len(summary["migrated"]) == 1
    assert len(summary["skipped"]) == 1
    assert len(summary["errors"]) == 1


def test_migrate_canvas_dir_idempotent_refuses_second_run(tmp_path: pathlib.Path):
    canvases = tmp_path / "canvases"
    canvases.mkdir()
    _write(canvases / "main.json", {"name": "main", "cards": [{"type": "terminal", "session_id": "x"}]})

    first = canvas_236.migrate_canvas_dir(canvases)
    assert len(first["migrated"]) == 1

    second = canvas_236.migrate_canvas_dir(canvases)
    # Now in new schema → skipped (no error, no rewrite, backup preserved).
    assert second["migrated"] == []
    assert len(second["skipped"]) == 1


def test_migrate_canvas_dir_missing_dir_returns_empty_summary(tmp_path: pathlib.Path):
    summary = canvas_236.migrate_canvas_dir(tmp_path / "does-not-exist")
    assert summary == {"migrated": [], "skipped": [], "errors": []}


# ── check_canvas_dir (startup probe) ────────────────────────


def test_check_canvas_dir_blocks_old_schema_without_backup(tmp_path: pathlib.Path):
    canvases = tmp_path / "canvases"
    canvases.mkdir()
    _write(canvases / "main.json", {"name": "main", "cards": [{"type": "terminal", "session_id": "x"}]})

    blocking = canvas_236.check_canvas_dir(canvases)
    assert len(blocking) == 1
    assert blocking[0].name == "main.json"


def test_check_canvas_dir_does_not_block_when_backup_exists(tmp_path: pathlib.Path):
    """A user who restored from a sidecar must not be wedged out of boot."""
    canvases = tmp_path / "canvases"
    canvases.mkdir()
    _write(canvases / "main.json", {"name": "main", "cards": [{"type": "terminal", "session_id": "x"}]})
    (canvases / "main.json.pre-236-backup").write_text("{}", encoding="utf-8")

    assert canvas_236.check_canvas_dir(canvases) == []


def test_check_canvas_dir_passes_clean_new_schema(tmp_path: pathlib.Path):
    canvases = tmp_path / "canvases"
    canvases.mkdir()
    _write(canvases / "main.json", {"name": "main", "cards": [{"type": "terminal", "card_id": "x"}]})
    assert canvas_236.check_canvas_dir(canvases) == []


def test_check_canvas_dir_skips_unreadable(tmp_path: pathlib.Path):
    canvases = tmp_path / "canvases"
    canvases.mkdir()
    (canvases / "broken.json").write_text("not json{", encoding="utf-8")
    assert canvas_236.check_canvas_dir(canvases) == []


def test_startup_error_template_names_cli_flag():
    msg = canvas_236.STARTUP_ERROR_TEMPLATE.format(path="/tmp/main.json")
    assert "--migrate-canvases" in msg
