"""Dev-mode config fixture.

When the server starts with --dev-config, it uses an isolated config directory
that is wiped and rebuilt from scratch on every startup. This prevents dev/test
sessions from touching the real user config in ~/.supreme-claudemander/.

Dev config dir: ~/.supreme-claudemander-dev/
"""

import json
import shutil
import pathlib

from loguru import logger

DEV_CONFIG_DIR = pathlib.Path.home() / ".supreme-claudemander-dev"

_FIXTURE_CONFIG = {
    "startup_script": "util-terminal",
    "default_canvas": "probe-qa",
    "probe_profiles": [],
    "sessions": {
        "orphan_timeout": 60,
        "scrollback_size": 65536,
        "tmux_persistence": True,
    },
    "util_container": {
        "name": "supreme-claudemander-util",
        "image": "supreme-claudemander-util:latest",
        "auto_start": True,
        "auto_stop": False,
        "mounts": {
            "~/.claude-profiles": "/profiles",
        },
    },
}

_FIXTURE_CANVASES = {
    "probe-qa": {
        "name": "probe-qa",
        "canvas_size": [3840, 2160],
        "cards": [],
    },
}


def setup_dev_config() -> pathlib.Path:
    """Wipe and rebuild the dev config directory. Returns the path."""
    if DEV_CONFIG_DIR.exists():
        shutil.rmtree(DEV_CONFIG_DIR)
        logger.info("Dev config: wiped {}", DEV_CONFIG_DIR)

    canvases_dir = DEV_CONFIG_DIR / "canvases"
    canvases_dir.mkdir(parents=True)

    (DEV_CONFIG_DIR / "config.json").write_text(json.dumps(_FIXTURE_CONFIG, indent=2), encoding="utf-8")

    for name, layout in _FIXTURE_CANVASES.items():
        (canvases_dir / f"{name}.json").write_text(json.dumps(layout, indent=2), encoding="utf-8")

    logger.info(
        "Dev config: seeded {} ({} canvas(es))",
        DEV_CONFIG_DIR,
        len(_FIXTURE_CANVASES),
    )
    return DEV_CONFIG_DIR
