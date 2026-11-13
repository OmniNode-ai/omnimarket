# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam field diff merge check: read the pairs, evaluate the base, ask the diff node (OMN-24121).

Exemplar followed: the OMN-24005 broker permission coverage check (an effect reads the
base revision with a scrubbed-env git subprocess, the pure node judges, the entry point
prints). Where it differs: the base side cannot be looked up from contract text, because
a pair's shape lives in model code, so the base revision's package source is extracted
with ``git archive`` into a temporary directory and the standalone ``pair_loader`` runs
there in a subprocess with that directory first on ``PYTHONPATH``; the base models it
imports are the base's own, never the working tree's.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import cast

import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.models.seam_field_diff.model_seam_field_diff import (
    ModelSeamFieldDiffRequest,
    ModelSeamFieldDiffVerdict,
    ModelSeamPairFacts,
)
from omnimarket.models.seam_field_diff.model_seam_field_diff_check import (
    ModelSeamFieldDiffCheckRequest,
    ModelSeamFieldDiffCheckResult,
)
from omnimarket.nodes.node_seam_field_diff_check_effect.handlers import pair_loader
from omnimarket.nodes.node_seam_field_diff_compute.handlers.handler_seam_field_diff import (
    HandlerSeamFieldDiff,
)

_NODE = "node_seam_field_diff_check_effect"


class SeamCheckUnreadableError(Exception):
    """The base revision or the contracts could not be read; the check answers exit 2."""


def contract_config() -> dict[str, object]:
    """The ``config:`` block of this node's contract: every value the check uses."""
    text = (
        resources.files(f"omnimarket.nodes.{_NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    loaded = yaml.safe_load(text)
    config = loaded.get("config") if isinstance(loaded, dict) else None
    if not isinstance(config, dict):
        raise ValueError(f"{_NODE}: contract config must be a mapping")
    return config


def _git(root: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=False,
        env=scrub_git_location_env(os.environ),
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise SeamCheckUnreadableError(f"git {' '.join(args)}: {detail}")
    return completed.stdout


def _strings(config: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = config.get(key)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise SeamCheckUnreadableError(
            f"contract config {key} must be a list of strings"
        )
    return tuple(value)


def _mapping(config: Mapping[str, object], key: str) -> dict[str, str]:
    value = config.get(key)
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise SeamCheckUnreadableError(
            f"contract config {key} must map names to strings"
        )
    return {str(k): str(v) for k, v in value.items()}


def _integer(config: Mapping[str, object], key: str) -> int:
    value = config.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise SeamCheckUnreadableError(
            f"contract config {key} must be a positive integer"
        )
    return value


def _installed_root(package: str) -> Path:
    spec = importlib.util.find_spec(package)
    if spec is None or not spec.origin:
        raise SeamCheckUnreadableError(f"package {package} is not installed")
    return Path(spec.origin).parent


class HandlerSeamFieldDiffCheck:
    """Effect: answer whether this change adds a producer-consumer shape difference."""

    def __init__(self, config_override: Mapping[str, object] | None = None) -> None:
        self._config_override = config_override

    def handle(
        self, request: ModelSeamFieldDiffCheckRequest
    ) -> ModelSeamFieldDiffCheckResult:
        try:
            return self._check(request)
        except SeamCheckUnreadableError as error:
            return ModelSeamFieldDiffCheckResult(
                exit_code=2,
                status="unreadable",
                output_lines=(f"seam field diff: unreadable: {error}",),
            )

    def _check(
        self, request: ModelSeamFieldDiffCheckRequest
    ) -> ModelSeamFieldDiffCheckResult:
        config = (
            dict(self._config_override)
            if self._config_override is not None
            else contract_config()
        )
        repo_root = Path(request.repo_root)
        repo_packages = _mapping(config, "repo_packages")
        contract_packages = _strings(config, "contract_packages")
        envelope_models = _strings(config, "envelope_models")
        depth = _integer(config, "nested_depth_limit")
        floor = _integer(config, "min_pairs_compared")
        timeout = _integer(config, "base_load_timeout_s")

        head_roots = {
            package: (
                repo_root / repo_packages[package]
                if package in repo_packages
                else _installed_root(package)
            )
            for package in contract_packages
        }
        head_loaded = pair_loader.collect_pairs(head_roots, envelope_models)
        base_loaded = self._base_pairs(
            repo_root,
            request.base_ref,
            repo_packages,
            {p: r for p, r in head_roots.items() if p not in repo_packages},
            envelope_models,
            timeout,
        )
        head_pairs = _pairs(head_loaded)
        base_pairs = _pairs(base_loaded)

        diff = HandlerSeamFieldDiff()
        base_verdict = diff.handle(
            ModelSeamFieldDiffRequest(
                head_pairs=base_pairs, base_finding_keys=(), nested_depth_limit=depth
            )
        )
        verdict = diff.handle(
            ModelSeamFieldDiffRequest(
                head_pairs=head_pairs,
                base_finding_keys=tuple(f.key for f in base_verdict.findings),
                nested_depth_limit=depth,
            )
        )
        unreadable = tuple(
            str(u) for u in cast("list[object]", head_loaded["unreadable"])
        )
        resolved = verdict.pairs_compared + len(verdict.adapter_skipped)
        if resolved < floor:
            raise SeamCheckUnreadableError(
                f"the scan resolved {resolved} pairs, fewer than the {floor} the "
                "contract config requires"
            )
        return _result(verdict, unreadable, request.base_ref)

    @staticmethod
    def _base_pairs(
        repo_root: Path,
        base_ref: str,
        repo_packages: Mapping[str, str],
        other_roots: Mapping[str, Path],
        envelope_models: tuple[str, ...],
        timeout_s: int,
    ) -> dict[str, object]:
        with tempfile.TemporaryDirectory(prefix="seam-field-diff-base-") as scratch:
            roots: dict[str, Path] = dict(other_roots)
            parents: list[str] = []
            for package, relative in repo_packages.items():
                if not _git(
                    repo_root, "ls-tree", "-r", "--name-only", base_ref, "--", relative
                ):
                    raise SeamCheckUnreadableError(
                        f"{base_ref} has no {relative} to read the base models from"
                    )
                archive = _git(
                    repo_root, "archive", "--format=tar", base_ref, "--", relative
                )
                with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                    tar.extractall(scratch, filter="data")
                destination = Path(scratch) / relative
                roots[package] = destination
                parents.append(str(destination.parent))
            command = [sys.executable, str(Path(pair_loader.__file__).resolve())]
            for package, root in roots.items():
                command += ["--root", f"{package}={root}"]
            for model in envelope_models:
                command += ["--envelope-model", model]
            environment = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            environment["PYTHONPATH"] = os.pathsep.join(parents)
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    check=False,
                    env=environment,
                    timeout=timeout_s,
                )
            except subprocess.TimeoutExpired as error:
                raise SeamCheckUnreadableError(
                    f"reading the base models took more than {timeout_s}s"
                ) from error
            if completed.returncode != 0:
                raise SeamCheckUnreadableError(
                    f"reading the base models failed: {completed.stderr.strip()[-300:]}"
                )
            try:
                loaded = json.loads(completed.stdout)
            except json.JSONDecodeError as error:
                raise SeamCheckUnreadableError(
                    f"the base model reader printed no JSON: {error}"
                ) from error
            if not isinstance(loaded, dict):
                raise SeamCheckUnreadableError(
                    "the base model reader printed no object"
                )
            return loaded


def _pairs(loaded: Mapping[str, object]) -> tuple[ModelSeamPairFacts, ...]:
    raw = loaded.get("pairs")
    if not isinstance(raw, list):
        raise SeamCheckUnreadableError("the pair reader returned no pairs list")
    return tuple(ModelSeamPairFacts.model_validate(p) for p in raw)


def _result(
    verdict: ModelSeamFieldDiffVerdict, unreadable: tuple[str, ...], base_ref: str
) -> ModelSeamFieldDiffCheckResult:
    lines = tuple(
        f"{f.issue.value}: {f.topic} | {_contracts(f.pair_key)} | "
        f"field {f.field}: {f.detail}"
        for f in verdict.new_findings
    )
    skipped = tuple(
        f"skipped (consumer adapter): {key.split('|')[0]} | {_contracts(key)}"
        for key in verdict.adapter_skipped
    )
    unread = tuple(f"unreadable: {reason}" for reason in unreadable)
    summary = (
        f"seam field diff: {verdict.status}; {verdict.pairs_compared} pairs compared, "
        f"{len(verdict.adapter_skipped)} skipped for a consumer adapter, "
        f"{len(unreadable)} unreadable, {len(verdict.new_findings)} new differences "
        f"since {base_ref}"
    )
    refused = verdict.status == "refused"
    return ModelSeamFieldDiffCheckResult(
        exit_code=1 if refused else 0,
        status="refused" if refused else "clean",
        pairs_compared=verdict.pairs_compared,
        adapter_skipped_count=len(verdict.adapter_skipped),
        unreadable_count=len(unreadable),
        adapter_skipped=verdict.adapter_skipped,
        unreadable=unreadable,
        new_findings=verdict.new_findings,
        output_lines=(*lines, *skipped, *unread, summary),
    )


def _contracts(pair_key: str) -> str:
    """'<producer contract> -> <consumer contract>' out of a pair key."""
    _, producer, consumer, *_ = pair_key.split("|")
    return f"{producer} -> {consumer}"


__all__ = ["HandlerSeamFieldDiffCheck", "SeamCheckUnreadableError", "contract_config"]
