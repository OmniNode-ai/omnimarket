# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A host home for the hook parse probe tests: the rendered script runs as a lab host runs it, under python3 and /bin/bash."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from omnimarket.nodes.node_hook_parse_probe_compute.handlers import (
    HandlerHookParseProbe,
)
from omnimarket.nodes.node_hook_parse_probe_compute.models import (
    ModelHookParseProbeRequest,
)

CLEAN = '#!/bin/bash\necho "$1"\n'
MARKET = "omninode-tools-dev"
KEY = f"onex@{MARKET}"
KEEP = {k: os.environ[k] for k in ("LD_LIBRARY_PATH",) if k in os.environ}


def home_with_installed_plugin(tmp: Path, *, key: str = "onex@omninode-tools") -> Path:
    """A host home holding one installed plugin whose hook tree carries a script that parses."""
    home = tmp / "home"
    root = home / ".claude" / "plugins" / "cache" / "onex" / "1.0.0"
    (root / "hooks" / "scripts").mkdir(parents=True)
    (root / "hooks" / "scripts" / "guard.sh").write_text(CLEAN, encoding="utf-8")
    (root / "hooks" / "hooks.json").write_text("{}", encoding="utf-8")
    listed = {"version": 2, "plugins": {key: [{"installPath": str(root)}]}}
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
        json.dumps(listed), encoding="utf-8"
    )
    return home


def volume_layout(tmp: Path, volumes_root: Path, volume: str) -> tuple[Path, Path]:
    """A lab host's layout: the host's registry is a link onto an external volume, and the directory marketplace sits under it."""
    home = home_with_installed_plugin(tmp, key=KEY)
    (home / "registry").symlink_to(volumes_root / volume / "Code" / "registry")
    market = home / "registry" / "plugin_host" / "plugins" / "dev-marketplace"
    known = {
        MARKET: {
            "source": {"source": "directory", "path": str(market)},
            "installLocation": str(market),
        }
    }
    (home / ".claude" / "plugins" / "known_marketplaces.json").write_text(
        json.dumps(known), encoding="utf-8"
    )
    return home, market


def mount(market: Path) -> None:
    """The volume is mounted: the marketplace checkout is there with its manifest and a plugin tree that parses."""
    (market / ".claude-plugin").mkdir(parents=True)
    (market / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": MARKET, "plugins": [{"name": "onex", "source": "./onex"}]}),
        encoding="utf-8",
    )
    (market / "onex" / "hooks" / "scripts").mkdir(parents=True)
    (market / "onex" / "hooks" / "scripts" / "guard.sh").write_text(
        CLEAN, encoding="utf-8"
    )


def render(**fields: object) -> str:
    request = ModelHookParseProbeRequest.model_validate(fields)
    return HandlerHookParseProbe().handle(request).script


def run_probe(home: Path, script: str, tmp: Path) -> str:
    """Run the rendered script as the lab runs it: ``python3`` first on PATH, the host's own /bin/bash."""
    bin_dir = tmp / "bin"
    bin_dir.mkdir(exist_ok=True)
    link = bin_dir / "python3"
    if not link.exists():
        link.symlink_to(sys.executable)
    done = subprocess.run(
        ["/bin/bash", "-c", script],
        capture_output=True,
        text=True,
        env={**KEEP, "HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin"},
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()
