# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The command line of the registry module tick unit installer (OMN-24108).

``onex-host-install-units <unit> [--dry-run|--install|...]``: one request, built from the
command line, the environment and the node's ``contract.yaml`` command_line block, run
through the node's ``RuntimeLocal`` with an in-memory event bus. Every option, mode flag,
environment variable and default comes from that block and the request model, never
hard-coded here.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import yaml
from omnibase_core.runtime.runtime_local import RuntimeLocal
from pydantic import ValidationError

from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
    ModelRegistryUnitsResult,
)


def _load_contract(contract: dict[str, Any] | None) -> dict[str, Any]:
    """The contract document, loaded from the file next to the package unless given."""
    if contract is not None:
        return contract
    with Path(__file__).with_name("contract.yaml").open(encoding="utf-8") as stream:
        document: dict[str, Any] = yaml.safe_load(stream)
    return document


def _fill_placeholders(template: str, values: Mapping[str, str]) -> str:
    """The template with its {home}, {registry_root} and {marketplace_root} placeholders
    filled in that order, using the values already resolved at that point."""
    for key in ("home", "registry_root", "marketplace_root"):
        if key in values:
            template = template.replace("{" + key + "}", values[key])
    return template


def run_request(request: ModelRegistryUnitsRequest) -> ModelRegistryUnitsResult:
    """Run one request through the node's contract with an in-memory event bus."""
    with TemporaryDirectory(prefix="host-registry-unit-") as scratch:
        input_path = Path(scratch) / "request.json"
        input_path.write_text(request.model_dump_json(), encoding="utf-8")
        runtime = RuntimeLocal(
            workflow_path=Path(__file__).with_name("contract.yaml"),
            input_path=input_path,
            state_root=Path(scratch) / "runtime",
            backend_overrides={"event_bus": "inmemory"},
            timeout=600,
        )
        runtime.run()
        if runtime.handler_result is None:
            raise RuntimeError("host installer runtime returned no result")
        return ModelRegistryUnitsResult.model_validate(runtime.handler_result)


def _set_option(value: str) -> str:
    """A ``--set`` argument: NAME=PATH, or an argparse error when it names no '='."""
    if "=" not in value:
        raise argparse.ArgumentTypeError("a --set argument is NAME=PATH")
    return value


def build_request(
    argv: list[str],
    environ: Mapping[str, str],
    home: str,
    contract: dict[str, Any] | None = None,
) -> ModelRegistryUnitsRequest:
    """The request the command line, the environment and the contract's command_line
    block name, or a ValueError naming the environment variable and the option to set."""
    doc = _load_contract(contract)
    command_line = doc["command_line"]
    modes: list[str] = command_line["modes"]
    required_inputs: list[str] = command_line["required_inputs"]
    env_inputs: dict[str, str] = command_line["env_inputs"]
    defaults: dict[str, str] = command_line["defaults"]

    parser = argparse.ArgumentParser(prog=command_line["program"], allow_abbrev=False)
    parser.add_argument("unit", nargs="?")
    for mode in modes:
        parser.add_argument(
            f"--{mode}",
            dest="mode",
            action="store_const",
            const=mode,
            default=None,
        )
    parser.add_argument("--set", action="append", type=_set_option, default=[])
    parser.add_argument("--list-units", action="store_true")
    parser.add_argument("--json", action="store_true")
    str_fields = [
        name
        for name, field in ModelRegistryUnitsRequest.model_fields.items()
        if name not in ("unit", "mode", "home") and field.annotation is str
    ]
    for field in str_fields:
        parser.add_argument(f"--{field.replace('_', '-')}", dest=field, default=None)
    args = parser.parse_args(argv)
    if args.unit is None:
        parser.error("a unit is required")

    resolved: dict[str, str] = {"home": home}
    values: dict[str, object] = {"unit": args.unit, "home": home}
    if args.mode is not None:
        values["mode"] = args.mode
    path_overrides: dict[str, str] = {}
    for item in args.set:
        name, _, value = item.partition("=")
        path_overrides[name] = value
    if path_overrides:
        values["path_overrides"] = path_overrides

    for field in str_fields:
        option = getattr(args, field)
        if option is not None:
            resolved[field] = option
            continue
        env_name = env_inputs.get(field)
        if env_name is not None and environ.get(env_name, ""):
            resolved[field] = environ[env_name]
            continue
        if field in defaults:
            resolved[field] = _fill_placeholders(defaults[field], resolved)

    for field in required_inputs:
        if field not in resolved:
            option = f"--{field.replace('_', '-')}"
            raise ValueError(
                f"set {env_inputs.get(field, field)} or pass {option} "
                "(the command has no default for it)"
            )
    for field in str_fields:
        if field in resolved:
            values[field] = resolved[field]
    return ModelRegistryUnitsRequest(**values)


def main(argv: list[str] | None = None) -> int:
    """Parse the command line, run the request it names and print its result."""
    args = sys.argv[1:] if argv is None else argv
    doc = _load_contract(None)
    program = doc["command_line"]["program"]

    probe = argparse.ArgumentParser(prog=program, allow_abbrev=False)
    probe.add_argument("--list-units", action="store_true")
    probe.add_argument("--json", action="store_true")
    flags, _rest = probe.parse_known_args(args)
    if flags.list_units:
        for name, spec in doc["units"].items():
            sys.stdout.write(f"{name}  {spec['platform']}\n")
        return 0

    try:
        request = build_request(
            args, os.environ, os.environ.get("HOME", ""), contract=doc
        )
    except (ValueError, ValidationError) as exc:
        sys.stderr.write(f"{program}: {exc}\n")
        return 1

    try:
        result = run_request(request)
    except RuntimeError as exc:
        sys.stderr.write(f"{program}: {exc}\n")
        return 1
    if flags.json:
        sys.stdout.write(result.model_dump_json(indent=1) + "\n")
    else:
        sys.stdout.write(result.stdout)
        if result.stderr:
            sys.stderr.write(result.stderr)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
