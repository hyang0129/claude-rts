"""Dev-mode config presets.

When the server starts with --dev-config [PRESET], it uses an isolated config
directory that is wiped and rebuilt from scratch on every startup. This prevents
dev/test sessions from touching the real user config in ~/.supreme-claudemander/.

Presets are stored as JSON fixture files in claude_rts/dev_presets/<name>/:
  config.json           — the config fixture
  canvases/<name>.json  — canvas layout fixtures

To add a new preset: create a new directory under dev_presets/ with config.json
and at least one canvas file.

Dev config dir: ~/.supreme-claudemander-dev/
"""

import json
import shutil
import pathlib

from loguru import logger

DEV_CONFIG_DIR = pathlib.Path.home() / ".supreme-claudemander-dev"

_PRESETS_DIR = pathlib.Path(__file__).parent / "dev_presets"


def list_presets() -> list[str]:
    """Return sorted list of available preset names."""
    if not _PRESETS_DIR.is_dir():
        return []
    return sorted(p.name for p in _PRESETS_DIR.iterdir() if p.is_dir() and (p / "config.json").exists())


def load_preset(name: str) -> dict:
    """Load a preset's config and canvases from disk.

    Returns {"config": dict, "canvases": {name: dict, ...}}
    """
    preset_dir = _PRESETS_DIR / name
    config_file = preset_dir / "config.json"
    if not config_file.exists():
        available = ", ".join(list_presets())
        raise ValueError(f"Unknown dev-config preset '{name}'. Available: {available}")

    config = json.loads(config_file.read_text(encoding="utf-8"))

    canvases = {}
    canvases_dir = preset_dir / "canvases"
    if canvases_dir.is_dir():
        for f in canvases_dir.glob("*.json"):
            canvases[f.stem] = json.loads(f.read_text(encoding="utf-8"))

    return {"config": config, "canvases": canvases}


# Legacy aliases for backwards compat in existing tests
_FIXTURE_CONFIG = load_preset("default")["config"]
_FIXTURE_CANVASES = load_preset("default")["canvases"]
PRESETS = {name: load_preset(name) for name in list_presets()}


def setup_dev_config(preset: str = "default") -> pathlib.Path:
    """Wipe and rebuild the dev config directory. Returns the path."""
    fixture = load_preset(preset)

    if DEV_CONFIG_DIR.exists():
        shutil.rmtree(DEV_CONFIG_DIR)
        logger.info("Dev config: wiped {}", DEV_CONFIG_DIR)

    canvases_dir = DEV_CONFIG_DIR / "canvases"
    canvases_dir.mkdir(parents=True)

    (DEV_CONFIG_DIR / "config.json").write_text(json.dumps(fixture["config"], indent=2), encoding="utf-8")

    for name, layout in fixture["canvases"].items():
        (canvases_dir / f"{name}.json").write_text(json.dumps(layout, indent=2), encoding="utf-8")

    logger.info(
        "Dev config: seeded {} with preset '{}' ({} canvas(es))",
        DEV_CONFIG_DIR,
        preset,
        len(fixture["canvases"]),
    )
    return DEV_CONFIG_DIR
