# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Aggregate contract ACL declarations and reconcile them with the broker."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.models.broker_acl import (
    EnumAclResourceType,
    EnumBrokerAclFindingCode,
    ModelBrokerAclDeriveRequest,
    ModelBrokerAclRow,
)
from omnimarket.models.broker_acl_reconcile import (
    ModelBrokerAclReconcileRequest,
    ModelBrokerAclReconcileResult,
)
from omnimarket.nodes.node_broker_acl_overlay_render_compute.handlers.handler_broker_acl_derive import (
    HandlerBrokerAclDerive,
)
from omnimarket.nodes.node_broker_acl_overlay_render_compute.overlay import (
    load_principals_overlay,
)
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers import driver_broker_acl
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.contract_topics import (
    load_topic_uses,
)

_NODE = "node_broker_acl_reconcile_effect"
_REPORT = re.compile(r"declared=(\d+) live=(\d+) missing=(\d+) undeclared=(\d+)")

Runner = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]


def contract() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{_NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    loaded = yaml.safe_load(text)
    if not isinstance(loaded, dict):
        raise ValueError("contract must be a mapping")
    return loaded


def declared_grants(declaration: Mapping[str, Any]) -> list[dict[str, str]]:
    """Every declared grant row as a flat mapping with its principal."""
    rows: list[dict[str, str]] = []
    for principal in declaration["principals"]:
        for grant in principal["grants"]:
            rows.append({"principal": principal["principal"], **grant})
    return rows


_PLACEHOLDER = re.compile(r"\$\{([A-Z0-9_]+)\}")


def render_declaration(
    declaration: Mapping[str, Any], environ: Mapping[str, str]
) -> dict[str, Any]:
    """The declaration with every ``${NAME}`` cell filled from ``environ``; an unset name raises."""
    missing: list[str] = []

    def fill(value: Any) -> Any:
        if isinstance(value, str):

            def one(match: re.Match[str]) -> str:
                found = environ.get(match.group(1), "")
                if not found:
                    missing.append(match.group(1))
                return found

            return _PLACEHOLDER.sub(one, value)
        if isinstance(value, list):
            return [fill(item) for item in value]
        if isinstance(value, dict):
            return {key: fill(item) for key, item in value.items()}
        return value

    rendered: dict[str, Any] = fill(dict(declaration))
    if missing:
        raise ValueError(f"unset: {', '.join(sorted(set(missing)))}")
    return rendered


def aggregate_declared_grants(nodes_root: Path | None = None) -> list[dict[str, str]]:
    """Merge this node's declaration with grant blocks from every packaged node.

    Malformed declarations fail closed and identify the contract to fix. Duplicate
    rows collapse before sorting by principal, resource type, pattern, name and operation.
    """
    root = (
        nodes_root
        if nodes_root is not None
        else Path(str(resources.files("omnimarket.nodes")))
    )
    own_path = resources.files(f"omnimarket.nodes.{_NODE}").joinpath("contract.yaml")
    blocks: list[tuple[str, object]] = [
        (str(own_path), contract()["broker_acl_declaration"]["principals"])
    ]
    for path in sorted(root.rglob("contract.yaml")):
        try:
            loaded = yaml.load(
                path.read_text(encoding="utf-8"),
                Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader),
            )
            if not isinstance(loaded, dict):
                raise ValueError("contract must be a mapping")
            if "broker_acl_grants" in loaded:
                blocks.append((str(path), loaded["broker_acl_grants"]))
        except (ValueError, OSError, yaml.YAMLError) as error:
            raise ValueError(f"{path}: {error}") from error
    keys = ("principal", "resource_type", "pattern", "name", "operation")
    rows: set[tuple[str, ...]] = set()
    for source, block in blocks:
        try:
            # Placeholder principals are checked again as concrete identities after rendering.
            checked = render_declaration(
                {"principals": block},
                dict.fromkeys(
                    _PLACEHOLDER.findall(yaml.safe_dump(block)), "User:placeholder"
                ),
            )
            driver_broker_acl.load_principals(checked["principals"])
            for row in declared_grants({"principals": block}):
                rows.add(tuple(row[key] for key in keys))
        except (ValueError, TypeError, KeyError) as error:
            raise ValueError(f"{source}: {error}") from error
    return [dict(zip(keys, row, strict=True)) for row in sorted(rows)]


def derived_grants(
    settings: Mapping[str, Any],
    declared: Sequence[Mapping[str, str]],
    environ: Mapping[str, str],
) -> tuple[list[dict[str, str]], int]:
    """The rows the contracts' topics need for the declared principals, and how many topics still have no row.

    No principals overlay (the setting is absent or the file is not found) derives nothing, so a deployment that does not
    declare principals applies exactly what it applied before. A derivation the apply tool would refuse raises.
    """
    scope = settings.get("derive_principals_scope")
    if not scope:
        return [], 0
    try:
        principals = load_principals_overlay(scope, environ)
    except NodeOverlayNotFoundError:
        return [], 0
    result = HandlerBrokerAclDerive().handle(
        ModelBrokerAclDeriveRequest(
            scope=scope,
            topic_uses=load_topic_uses(),
            principals=principals,
            declared_rows=tuple(
                ModelBrokerAclRow(**row)
                for row in declared
                if row["resource_type"] == EnumAclResourceType.TOPIC.value
            ),
        )
    )
    refusing = sorted(
        {
            finding.code.value
            for finding in result.findings
            if finding.code != EnumBrokerAclFindingCode.TOPIC_WITHOUT_ROW
        }
    )
    if refusing:
        raise ValueError(f"derived rows refused: {', '.join(refusing)}")
    return [
        {
            "principal": row.principal,
            "resource_type": row.resource_type.value,
            "pattern": row.pattern.value,
            "name": row.name,
            "operation": row.operation.value,
        }
        for row in result.rows
    ], len(result.uncovered_topics)


def _run(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv), capture_output=True, text=True, timeout=timeout, check=False
    )


class HandlerBrokerAclReconcile:
    def __init__(
        self,
        runner: Runner = _run,
        environ: Mapping[str, str] | None = None,
        remote_runner: driver_broker_acl.RemoteRunner | None = None,
    ) -> None:
        self._runner = runner
        self._remote_runner = remote_runner
        self._environ = os.environ if environ is None else environ

    def handle(
        self, request: ModelBrokerAclReconcileRequest
    ) -> ModelBrokerAclReconcileResult:
        loaded = contract()
        settings = loaded["broker_acl_reconcile"]
        target = self._environ.get(settings["ssh_target_env"], "")
        if not target:
            return ModelBrokerAclReconcileResult(
                mode=request.mode,
                exit_code=2,
                error=f"{settings['ssh_target_env']} is not set",
            )
        # The program comes only from operator configuration; a bus request never
        # carries an argv. Without configured command, the packaged driver runs.
        command = shlex.split(self._environ.get(settings["reconcile_command_env"], ""))
        if not command:
            return self._apply_driver(request, loaded, target)
        try:
            rendered = render_declaration(
                loaded["broker_acl_declaration"], self._environ
            )
        except ValueError as error:
            return ModelBrokerAclReconcileResult(
                mode=request.mode, exit_code=2, error=str(error)
            )
        with tempfile.TemporaryDirectory() as scratch:
            declaration_path = Path(scratch) / "declaration.yaml"
            declaration_path.write_text(
                yaml.safe_dump(rendered, sort_keys=False),
                encoding="utf-8",
            )
            argv = [
                *command,
                "--declaration",
                str(declaration_path),
                "--ssh-target",
                target,
                "apply",
            ]
            if request.mode == "dry_run":
                argv.append("--dry-run")
            try:
                completed = self._runner(argv, float(settings["timeout_seconds"]))
            except (OSError, subprocess.TimeoutExpired) as error:
                return ModelBrokerAclReconcileResult(
                    mode=request.mode, exit_code=2, error=str(error)
                )
        return self._result(request, completed)

    def _apply_driver(
        self,
        request: ModelBrokerAclReconcileRequest,
        loaded: Mapping[str, Any],
        target: str,
    ) -> ModelBrokerAclReconcileResult:
        try:
            declaration = dict(loaded["broker_acl_declaration"])
            declared = aggregate_declared_grants()
            derived, uncovered = derived_grants(
                loaded["broker_acl_reconcile"], declared, self._environ
            )
            keys = ("principal", "resource_type", "pattern", "name", "operation")
            declaration["rows"] = [
                dict(zip(keys, row, strict=True))
                for row in sorted(
                    {tuple(row[key] for key in keys) for row in (*declared, *derived)}
                )
            ]
            rendered = render_declaration(declaration, self._environ)
            timeout = float(loaded["broker_acl_reconcile"]["timeout_seconds"])
            connection = driver_broker_acl.AclConnection.from_mapping(
                rendered, target, timeout
            )
            grants = [driver_broker_acl.AclGrant(**row) for row in rendered["rows"]]

            def run_remote(ssh_target: str, script: str) -> str:
                return driver_broker_acl.run_remote(ssh_target, script, timeout=timeout)

            output = driver_broker_acl.apply(
                grants, connection, request.mode, self._remote_runner or run_remote
            )
            reports = _REPORT.findall(output)
            code = int(bool(int(reports[-1][2])))
            return self._result(
                request, subprocess.CompletedProcess([], code, output, "")
            ).model_copy(
                update={"derived": len(derived), "uncovered_topics": uncovered}
            )
        except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            return ModelBrokerAclReconcileResult(
                mode=request.mode, exit_code=2, error=str(error)
            )

    @staticmethod
    def _result(
        request: ModelBrokerAclReconcileRequest,
        completed: subprocess.CompletedProcess[str],
    ) -> ModelBrokerAclReconcileResult:
        reports = _REPORT.findall(completed.stdout)
        # The last report line is the readback after the apply.
        counts = [int(value) for value in reports[-1]] if reports else [None] * 4
        missing_rows = tuple(
            line.removeprefix("MISSING ")
            for line in completed.stdout.splitlines()
            if line.startswith("MISSING ")
        )
        return ModelBrokerAclReconcileResult(
            mode=request.mode,
            exit_code=completed.returncode,
            declared=counts[0],
            live=counts[1],
            missing=counts[2],
            undeclared=counts[3],
            missing_rows=missing_rows if counts[2] else (),
            output=completed.stdout,
            error=completed.stderr.strip()[-400:],
        )
