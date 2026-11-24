# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The half of the C17 collector that runs inside the clean tool environment (OMN-26044).

The collector streams this file on stdin to the INSTALLED tool environment's interpreter after the guide's install line has
run, so it imports the standard library and whatever that install put on the interpreter, nothing from this repository: what
it sees is what a customer's machine sees. It measures and never grades; the node grades the JSON document it prints.

A broken ``onex.cli`` entry point is planted as a real distribution (a dist-info directory in the tool environment's own
site-packages, which is where a broken third-party extension lands). The installed core hides every command group its
declaration does not mark customer, so the planted command answers "No such command" however it is broken, unless the run
turns on the core's own operator switch. The driver reads that switch's name from the installed core and runs every broken
case with it on, and records both the name and that it was on.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata as md
import json
import os
import shutil
import subprocess
import sys
import sysconfig
from collections.abc import Mapping
from pathlib import Path
from typing import Any

OUTPUT_CAP = 20000
BROKEN_DIST = "c17-broken-probe"
BROKEN_CASES = (
    # (entry-point name, target, what kind of breakage it is)
    ("c17-broken-probe-import", "c17_probe_no_such_module:group", "import_error"),
    ("c17-broken-probe-type", "json:dumps", "wrong_type"),
)
SWITCH_MODULE = "omnibase_core.models.cli.model_cli_command_surface"
SWITCH_ATTR = "OPERATOR_SWITCH_ENV"
SWITCH_ON = "1"
CUSTOMER_COMMANDS = ("delegate", "info", "local")


def run(argv: list[str], env: Mapping[str, str] | None = None) -> dict[str, Any]:
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
            env=None if env is None else dict(env),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "argv": argv,
            "rc": None,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
        }
    return {
        "argv": argv,
        "rc": done.returncode,
        "stdout": done.stdout[-OUTPUT_CAP:],
        "stderr": done.stderr[-OUTPUT_CAP:],
    }


def interpreter() -> dict[str, Any]:
    cfg = Path(sys.prefix) / "pyvenv.cfg"
    fields: dict[str, str] = {}
    if cfg.is_file():
        for line in cfg.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep:
                fields[key.strip()] = value.strip()
    return {
        "pyvenv_cfg": fields,
        "sys_version": ".".join(str(part) for part in sys.version_info[:3]),
        "executable": sys.executable,
    }


def installed_versions(names: tuple[str, ...]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in names:
        try:
            out[name] = md.version(name)
        except md.PackageNotFoundError:
            continue
    return out


def operator_switch_env() -> str | None:
    """The environment variable the installed core names for showing every command group, or None when it cannot be read."""
    try:
        module = __import__(SWITCH_MODULE, fromlist=[SWITCH_ATTR])
        value = getattr(module, SWITCH_ATTR)
    except Exception:
        return None
    return value if isinstance(value, str) and value else None


def _onex_command() -> Any:
    matches = [
        ep for ep in md.entry_points(group="console_scripts") if ep.name == "onex"
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected exactly one 'onex' console script, found {len(matches)}"
        )
    return matches[0].load()


def _walk(cmd: Any, path: list[str], parent: Any, out: list[dict[str, Any]]) -> None:
    # Resolved at call time from the installed tool environment: click is the installed CLI's, not this node's dependency.
    click = importlib.import_module("click")

    ctx = click.Context(cmd, info_name=path[-1], parent=parent)
    params = []
    for param in cmd.params:
        default = param.default
        params.append(
            {
                "kind": type(param).__name__,
                "name": param.name,
                "opts": list(param.opts)
                + list(getattr(param, "secondary_opts", []) or []),
                "envvar": param.envvar,
                "default": "<callable>" if callable(default) else repr(default),
                "help": getattr(param, "help", None),
            }
        )
    out.append({"path": " ".join(path), "help": cmd.get_help(ctx), "params": params})
    if isinstance(cmd, click.Group):
        for name in cmd.list_commands(ctx):
            sub = cmd.get_command(ctx, name)
            if sub is not None:
                _walk(sub, [*path, name], ctx, out)


def cli_surface() -> dict[str, Any]:
    """Every command the default view of the installed ``onex`` exposes, with every option's names, envvars, default and help."""
    try:
        commands: list[dict[str, Any]] = []
        _walk(_onex_command(), ["onex"], None, commands)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "commands": []}
    return {"error": None, "commands": commands}


def plant_broken(
    onex: str,
    purelib: Path,
    env: Mapping[str, str],
    switch_env: str | None,
) -> dict[str, Any]:
    """Plant each broken ``onex.cli`` entry point as a distribution, ask ``onex``, remove it and prove the removal restored ``onex``.

    Every case runs with ``switch_env`` set when the installed core names one, so a case that still answers "No such command"
    is a run that did not take the switch. ``ran_with_switch`` records whether it was on, never a guess.
    """
    case_env = dict(env)
    if switch_env:
        case_env[switch_env] = SWITCH_ON
    dist_info = purelib / f"{BROKEN_DIST.replace('-', '_')}-0.0.0.dist-info"
    record: dict[str, Any] = {
        "switch_env": switch_env,
        "ran_with_switch": bool(switch_env),
        "before": run([onex, "--help"], env),
        "cases": [],
    }
    for name, target, kind in BROKEN_CASES:
        dist_info.mkdir(parents=True, exist_ok=True)
        (dist_info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {BROKEN_DIST}\nVersion: 0.0.0\n"
        )
        (dist_info / "entry_points.txt").write_text(f"[onex.cli]\n{name} = {target}\n")
        (dist_info / "RECORD").write_text("")
        try:
            result = run([onex, name], case_env)
        finally:
            shutil.rmtree(dist_info, ignore_errors=True)
        record["cases"].append(
            {"entry_point": name, "target": target, "kind": kind, "result": result}
        )
    record["after"] = run([onex, "--help"], env)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--onex", required=True, help="path to the installed onex executable"
    )
    parser.add_argument("--packages", nargs="*", default=[])
    args = parser.parse_args()
    # Importing the CLI to walk it may print; the record must be the only thing on stdout.
    record_stream, sys.stdout = sys.stdout, sys.stderr
    env = dict(os.environ)
    document = {
        "interpreter": interpreter(),
        "versions": installed_versions(tuple(args.packages)),
        "commands": {
            "version": run([args.onex, "--version"], env),
            **{
                f"{name}_help": run([args.onex, name, "--help"], env)
                for name in CUSTOMER_COMMANDS
            },
        },
        "cli_surface": cli_surface(),
        "broken": plant_broken(
            args.onex,
            Path(sysconfig.get_paths()["purelib"]),
            env,
            operator_switch_env(),
        ),
    }
    json.dump(document, record_stream, default=str)
    record_stream.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
