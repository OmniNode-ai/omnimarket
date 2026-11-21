# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""No registry-root variable on the customer-facing `onex` option surface (OMN-25632, C17).

The C17 customer-surface probe walks every command of the installed `onex` and fails
its parameter audit on a parameter that names ``$OMNI_HOME`` in help or binds it as an
environment variable: OMNI_HOME is the internal registry root, and the customer's
workspace variable is OMNIBASE_PATH. Against omnimarket 0.4.324 the audit read eight
findings, all on ``onex setup --workspace`` and ``onex repo-state unit install|render
--omni-root``. This test walks the same two surfaces the way the probe's driver does
(click's own parameter table and rendered help) and asserts none of the probe's
OMNI_HOME patterns appear.
"""

from __future__ import annotations

import re
from typing import Any

import click
import pytest

from omnimarket.nodes.node_onex_setup_effect.cli import setup_command
from omnimarket.nodes.node_repo_state_query_effect.handlers.adapter_repo_state_query_serve import (
    repo_state_group,
)

pytestmark = pytest.mark.unit

# The probe's own patterns (omniclaude scripts/skill_monorepo_ref_patterns.json,
# ids omni_home_bare, omni_home_braced, omni_home_envvar_binding).
_BARE = re.compile(r"\$OMNI_HOME")
_BRACED = re.compile(r"\$\{OMNI_HOME")


def _walk(cmd: click.Command, path: list[str]) -> list[tuple[str, click.Command]]:
    ctx = click.Context(cmd, info_name=path[-1])
    found = [(" ".join(path), cmd)]
    if isinstance(cmd, click.Group):
        for name in cmd.list_commands(ctx):
            sub = cmd.get_command(ctx, name)
            if sub is not None:
                found += _walk(sub, [*path, name])
    return found


def _surface() -> list[tuple[str, click.Command]]:
    return _walk(setup_command, ["onex", "setup"]) + _walk(
        repo_state_group, ["onex", "repo-state"]
    )


def _findings() -> list[str]:
    out: list[str] = []
    for path, cmd in _surface():
        ctx = click.Context(cmd, info_name=path.split()[-1])
        texts: list[tuple[str, Any]] = [(f"{path} --help", cmd.get_help(ctx))]
        for param in cmd.params:
            texts.append((f"{path} {param.name} (help)", getattr(param, "help", None)))
            envvars = param.envvar if isinstance(param.envvar, list) else [param.envvar]
            for envvar in envvars:
                if envvar == "OMNI_HOME":
                    out.append(f'{path} {param.name}: envvar="OMNI_HOME"')
        for where, text in texts:
            if text and (_BARE.search(str(text)) or _BRACED.search(str(text))):
                out.append(f"{where}: names $OMNI_HOME")
    return out


def test_walk_reaches_the_surfaces_the_probe_flagged() -> None:
    """Positive control: the walk sees the options the audit found, so a zero is a real zero."""
    paths = {path for path, _ in _surface()}
    assert "onex setup" in paths
    assert "onex repo-state unit install" in paths
    assert "onex repo-state unit render" in paths
    options = {p.name for _, c in _surface() for p in c.params}
    assert {"workspace", "omni_root"} <= options


def test_no_omni_home_on_the_customer_option_surface() -> None:
    assert _findings() == []


def test_the_seeded_control_is_caught() -> None:
    """Seed `$OMNI_HOME` into a copy of one option's help and check the scan catches it."""
    seeded = click.Command(
        "seed", params=[click.Option(["--x"], help="root is $OMNI_HOME")]
    )
    ctx = click.Context(seeded, info_name="seed")
    assert _BARE.search(seeded.params[0].help or "")
    assert _BARE.search(seeded.get_help(ctx))


def test_setup_workspace_still_reads_omnibase_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The customer's workspace variable keeps working after OMNI_HOME is dropped."""
    option = next(p for p in setup_command.params if p.name == "workspace")
    envvars = option.envvar if isinstance(option.envvar, list) else [option.envvar]
    assert "OMNIBASE_PATH" in envvars
