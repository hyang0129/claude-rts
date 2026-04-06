"""File-based persistence for Claude API credentials (module: cred_store).

Credentials file: ~/.supreme-claudemander/credentials.json
"""

import json
import threading
import uuid

from loguru import logger

from claude_rts.config import CONFIG_DIR, ensure_dirs

CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"
DEFAULT_CREDENTIALS: dict = {"priority": None, "credentials": []}

# Protects all read-modify-write sequences on the credentials file.
_lock = threading.RLock()

_USAGE_FIELDS = (
    "five_hour_pct",
    "burn_rate",
    "five_hour_resets",
    "seven_day_pct",
    "usage_7d",
    "quota",
    "refresh_at",
)


# ── Persistence ─────────────────────────────────────────


def read_credentials() -> dict:
    """Read credentials from disk, returning defaults if missing or corrupt."""
    with _lock:
        if CREDENTIALS_FILE.exists():
            try:
                data = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
                logger.debug("Loaded credentials from {}", CREDENTIALS_FILE)
                return data
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to read credentials, using defaults: {}", exc)
        return {**DEFAULT_CREDENTIALS, "credentials": []}


def write_credentials(data: dict) -> dict:
    """Write credentials to disk. Returns the written data."""
    ensure_dirs()
    CREDENTIALS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info("Wrote credentials to {}", CREDENTIALS_FILE)
    return data


# ── Key masking ─────────────────────────────────────────


def mask_key(key: str) -> str:
    """Return a masked representation of an API key.

    - None or empty  → ""
    - len <= 11      → "sk-ant-...xxxx"  (fully masked)
    - else           → first 7 chars + "..." + last 4 chars
    """
    if not key:
        return ""
    if len(key) <= 11:
        return "sk-ant-...xxxx"
    return key[:7] + "..." + key[-4:]


# ── Internal helpers ─────────────────────────────────────


def _mask_credential(cred: dict) -> dict:
    """Return a copy of cred with the raw 'key' field removed."""
    return {k: v for k, v in cred.items() if k != "key"}


# ── CRUD ────────────────────────────────────────────────


def add_credential(label: str, key: str, profile: str | None = None) -> dict:
    """Add a new credential record.

    Validates label and key are non-empty, generates a unique id, appends to
    the credentials list, persists to disk, and returns the record WITHOUT the
    raw key field.
    """
    if not label or not isinstance(label, str):
        raise ValueError("label must be a non-empty string")
    if not key or not isinstance(key, str):
        raise ValueError("key must be a non-empty string")

    cred_id = f"cred-{uuid.uuid4().hex[:8]}"
    record: dict = {
        "id": cred_id,
        "label": label,
        "profile": profile,
        "key": key,
        "key_hint": mask_key(key),
        "usage_7d": 0,
        "quota": None,
        "refresh_at": None,
        "five_hour_pct": None,
        "burn_rate": None,
        "five_hour_resets": None,
        "seven_day_pct": None,
    }

    with _lock:
        data = read_credentials()
        data["credentials"].append(record)
        write_credentials(data)
    logger.info("Added credential '{}' (id={})", label, cred_id)

    return _mask_credential(record)


def delete_credential(cred_id: str) -> bool:
    """Delete a credential by id. Clears priority if it matched.

    Returns True if found and deleted, False otherwise.
    """
    with _lock:
        data = read_credentials()
        original_len = len(data["credentials"])
        data["credentials"] = [c for c in data["credentials"] if c["id"] != cred_id]

        if len(data["credentials"]) == original_len:
            logger.debug("delete_credential: id={} not found", cred_id)
            return False

        if data.get("priority") == cred_id:
            data["priority"] = None
            logger.debug("Cleared priority because credential {} was deleted", cred_id)

        write_credentials(data)

    logger.info("Deleted credential id={}", cred_id)
    return True


def set_priority(cred_id: str) -> bool:
    """Set the priority credential by id.

    Returns True if the id exists and priority was set, False otherwise.
    """
    with _lock:
        data = read_credentials()
        ids = {c["id"] for c in data["credentials"]}
        if cred_id not in ids:
            logger.debug("set_priority: id={} not found", cred_id)
            return False

        data["priority"] = cred_id
        write_credentials(data)

    logger.info("Set priority credential to id={}", cred_id)
    return True


def get_priority() -> dict | None:
    """Return the priority credential (masked, no raw key), or None."""
    data = read_credentials()
    priority_id = data.get("priority")
    if not priority_id:
        return None

    for cred in data["credentials"]:
        if cred["id"] == priority_id:
            return _mask_credential(cred)

    logger.debug("get_priority: priority id={} not found in credentials list", priority_id)
    return None


def update_credential_usage(cred_id: str, usage_data: dict) -> bool:
    """Merge usage fields from usage_data into the credential record.

    Accepted fields: five_hour_pct, burn_rate, five_hour_resets, seven_day_pct,
    usage_7d, quota, refresh_at.

    Returns True if found and updated, False otherwise.
    """
    with _lock:
        data = read_credentials()
        for cred in data["credentials"]:
            if cred["id"] == cred_id:
                for field in _USAGE_FIELDS:
                    if field in usage_data:
                        cred[field] = usage_data[field]
                write_credentials(data)
                logger.debug("Updated usage for credential id={}", cred_id)
                return True

    logger.debug("update_credential_usage: id={} not found", cred_id)
    return False


def update_usage_by_profile(profile_name: str, usage_data: dict) -> bool:
    """Atomically look up a credential by profile name and update its usage stats.

    Combines the profile→id lookup and the usage update under a single lock
    acquisition to avoid TOCTOU races.

    Returns True if a matching credential was found and updated, False otherwise.
    """
    with _lock:
        data = read_credentials()
        for cred in data.get("credentials", []):
            if cred.get("profile") == profile_name:
                for field in _USAGE_FIELDS:
                    if field in usage_data:
                        cred[field] = usage_data[field]
                write_credentials(data)
                logger.debug(
                    "Updated usage for credential with profile={}", profile_name
                )
                return True
    logger.debug(
        "update_usage_by_profile: no credential with profile={}", profile_name
    )
    return False


def _rank_from_data(data: dict) -> list[dict]:
    """Build ranked credential list from an already-loaded data dict (no file I/O)."""
    priority_id = data.get("priority")

    def _sort_key(cred: dict):
        br = cred.get("burn_rate")
        return (1, 0) if br is None else (0, br)

    ranked = sorted(data["credentials"], key=_sort_key)
    result = []
    for cred in ranked:
        masked = _mask_credential(cred)
        masked["is_priority"] = cred["id"] == priority_id
        result.append(masked)
    return result


def list_credentials_ranked() -> list[dict]:
    """Return all credentials sorted by ascending burn_rate (None values last).

    Each record has the raw key removed and an 'is_priority' boolean added.
    """
    return _rank_from_data(read_credentials())


def get_credentials_response() -> dict:
    """Return priority id + ranked list in a single file read.

    Used by the list API handler to avoid two separate reads that could diverge.
    """
    data = read_credentials()
    return {"priority": data.get("priority"), "credentials": _rank_from_data(data)}
