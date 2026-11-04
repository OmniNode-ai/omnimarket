# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Apply the contract's declared consumer-group DELETE ACL rows to the dev broker.

The rows, the reconcile command and the ssh-target variable are all read from this
node's ``contract.yaml``. The handler renders the rows into a declaration file in the
lab broker ACL reconcile command's format and runs that command's own
``apply`` (``--dry-run`` first): it lists the live rows, creates only the missing ones
and lists again, so the readback is the broker's listing, not a local assumption.
"""

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

from omnimarket.models.broker_acl_reconcile import (
    ModelBrokerAclReconcileRequest,
    ModelBrokerAclReconcileResult,
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


def _run(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv), capture_output=True, text=True, timeout=timeout, check=False
    )


class HandlerBrokerAclReconcile:
    def __init__(
        self,
        runner: Runner = _run,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._runner = runner
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
        # carries an argv.
        command = shlex.split(self._environ.get(settings["reconcile_command_env"], ""))
        if not command:
            return ModelBrokerAclReconcileResult(
                mode=request.mode,
                exit_code=2,
                error=f"{settings['reconcile_command_env']} is not set",
            )
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
