# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Customer SCM help and parameter metadata must not name the internal workspace.

Walk every command, including hidden refusals, as a customer-surface audit does.
Clone defaults must honor the declared public root while preserving older hosts.
"""

from __future__ import annotations

import re
from pathlib import Path

import click
import pytest

from omnimarket.handlers.scm.handler_scm_dispatch import (
    SCM_LOCAL_VERBS,
    SCM_REFUSED_VERBS,
    SCM_VERBS,
)
from omnimarket.handlers.scm.handler_scm_local_state import default_clone_path
from omnimarket.handlers.scm.scm_cli import scm_group

pytestmark = pytest.mark.unit

_VAR = "OMNI_HOME"
_ROOT = "ONEX_SCM_CLONE_ROOT"
_REPO = "OmniNode-ai/omnimarket"
_PATTERNS = tuple(
    re.compile(re.escape(form))
    for form in (
        "$" + _VAR,
        "${" + _VAR,
        f'envvar="{_VAR}"',
        "--" + _VAR.lower().replace("_", "-"),
        _VAR + ":",
    )
)


def _walk(
    cmd: click.Command,
    path: tuple[str, ...],
    parent: click.Context | None,
    out: list[tuple[str, str]],
) -> None:
    ctx = click.Context(cmd, info_name=path[-1], parent=parent)
    where = " ".join(path)
    out.extend(
        ((where + ":get_help", cmd.get_help(ctx)), (where + ":help", cmd.help or ""))
    )
    if cmd.callback is not None:
        out.append((where + ":docstring", cmd.callback.__doc__ or ""))
    for index, param in enumerate(cmd.params):
        location = f"{where}:param:{index}"
        names = [param.name or "", *param.opts, *param.secondary_opts]
        for name in names:
            rendered = "$" + name.lstrip("-").replace("-", "_").upper()
            out.append((location + ":name", rendered))
        bindings = param.envvar
        envvars = [bindings] if isinstance(bindings, str) else bindings or []
        for envvar in envvars:
            out.append((location + ":envvar", f'envvar="{envvar}"'))
            out.append((location + ":envvar", "$" + envvar))
        default = "<callable>" if callable(param.default) else repr(param.default)
        out.append((location + ":default", default))
        out.append((location + ":metavar", param.make_metavar(ctx)))
        if isinstance(param, click.Option):
            out.append((location + ":help", param.help or ""))
            if isinstance(param.show_default, str):
                out.append((location + ":show_default", param.show_default))
    if isinstance(cmd, click.Group):
        for name in cmd.list_commands(ctx):
            child = cmd.get_command(ctx, name)
            assert child is not None, (*path, name)
            _walk(child, (*path, name), ctx, out)


def _surface(cmd: click.Command) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    _walk(cmd, ("scm",), None, out)
    return out


def _findings(surface: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [
        (where, match.group())
        for where, text in surface
        for pattern in _PATTERNS
        for match in pattern.finditer(text)
    ]


def test_walk_reaches_every_verb() -> None:
    surface = _surface(scm_group)
    reached = {
        where.removesuffix(":get_help")
        for where, _ in surface
        if where.endswith(":get_help")
    }
    expected = {"scm", "scm verbs"}
    expected.update("scm " + spec.verb for spec in SCM_VERBS)
    expected.update("scm " + spec.verb for spec in SCM_LOCAL_VERBS)
    expected.update("scm " + spec.verb for spec in SCM_REFUSED_VERBS)
    assert expected <= reached
    assert sum(where.endswith(":default") for where, _ in surface) > 0


def test_no_scm_surface_names_the_internal_workspace_variable() -> None:
    findings = _findings(_surface(scm_group))
    assert not findings, "\n".join(f"{where}: {form}" for where, form in findings)


def test_positive_control_a_seeded_parameter_is_caught() -> None:
    group = click.Group("scm", commands=dict(scm_group.commands), help=scm_group.help)
    option = "--" + _VAR.lower().replace("_", "-")
    params: list[click.Parameter] = [
        click.Option([option]),
        click.Option(["--clone"], help="default $" + _VAR + "/<repo>"),
    ]
    group.add_command(click.Command("seeded", params=params))
    findings = _findings(_surface(group))
    assert ("scm seeded:param:0:name", "$" + _VAR) in findings
    assert ("scm seeded:param:1:help", "$" + _VAR) in findings


def test_clone_default_is_the_declared_clone_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "omnimarket" / ".git").mkdir(parents=True)
    monkeypatch.setenv(_ROOT, str(tmp_path))
    monkeypatch.delenv(_VAR, raising=False)
    assert default_clone_path(_REPO) == tmp_path / "omnimarket"


def test_clone_help_names_the_declared_clone_root() -> None:
    ctx = click.Context(scm_group)
    command = scm_group.get_command(ctx, "pr-view")
    assert command is not None
    clone = next(param for param in command.params if "--clone" in param.opts)
    assert isinstance(clone, click.Option)
    assert "$" + _ROOT + "/<repo>" in (clone.help or "")


def test_declared_clone_root_wins_and_does_not_fall_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    declared, workspace = tmp_path / "declared", tmp_path / "workspace"
    declared.mkdir()
    (workspace / "omnimarket" / ".git").mkdir(parents=True)
    monkeypatch.setenv(_ROOT, str(declared))
    monkeypatch.setenv(_VAR, str(workspace))
    assert default_clone_path(_REPO) is None


def test_hosts_without_the_declared_root_keep_their_clone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "omnimarket" / ".git").mkdir(parents=True)
    monkeypatch.delenv(_ROOT, raising=False)
    monkeypatch.setenv(_VAR, str(tmp_path))
    assert default_clone_path(_REPO) == tmp_path / "omnimarket"


def test_no_setting_means_no_clone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ROOT, raising=False)
    monkeypatch.delenv(_VAR, raising=False)
    assert default_clone_path(_REPO) is None
