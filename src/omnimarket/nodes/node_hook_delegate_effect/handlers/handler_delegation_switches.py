# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read and write each delegation hook's switch (OMN-23413, operator 2026-11-15)."""

import json
import os
import tempfile
from pathlib import Path

from omnibase_core.cli.cli_user_config import user_config_path
from pydantic import ValidationError

from omnimarket.nodes.node_hook_delegate_effect.models.model_delegation_switches import (
    ModelDelegationSwitches,
)

#: An install from before the switches has no file: both hooks delegate.
_DEFAULT = ModelDelegationSwitches()
#: A file onex cannot read may hold a switch the user turned off, so nothing delegates.
_UNREADABLE = ModelDelegationSwitches(prompts=False, subagents=False)


def switches_path() -> Path:
    return user_config_path().parent / "delegation.json"


def read_switches() -> ModelDelegationSwitches:
    try:
        text = switches_path().read_text()
    except FileNotFoundError:
        return _DEFAULT
    except OSError:
        return _UNREADABLE
    try:
        return ModelDelegationSwitches.model_validate(json.loads(text))
    except (ValueError, ValidationError):
        return _UNREADABLE


def write_switches(switches: ModelDelegationSwitches) -> None:
    """Replace the file whole, so a hook never reads half of it."""
    path = switches_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".delegation-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(switches.model_dump(mode="json"), stream)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
