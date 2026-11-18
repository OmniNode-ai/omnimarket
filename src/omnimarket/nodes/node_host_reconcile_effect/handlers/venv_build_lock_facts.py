# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build-lock facts for the dispatch venv (OMN-19998)."""

import hashlib
import json
import tempfile
from pathlib import Path

from omnibase_core.errors.model_onex_error import ModelOnexError
from omnibase_core.models.venv_build_lock.model_venv_build_lock_document import (
    ModelVenvBuildLockDocument,
)
from omnibase_core.models.venv_build_lock.model_venv_build_lock_surface import (
    ModelVenvBuildLockSurface,
)
from omnibase_core.validation.validator_venv_build_lock import ValidatorVenvBuildLock

from omnimarket.models.model_host_reconcile import (
    ModelSurfaceFact,
    ModelSurfaceIndeterminateFact,
    ModelSurfaceUnhealthyFact,
    ModelVenvBuildLockFact,
    ModelVenvBuildLockIssue,
)


def _normalise(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def _scan_site_packages(site_packages: Path) -> tuple[dict[str, str], dict[str, str]]:
    installed: dict[str, str] = {}
    installed_git: dict[str, str] = {}
    if not site_packages.is_dir():
        return installed, installed_git
    for dist_info in sorted(site_packages.glob("*.dist-info")):
        if not dist_info.is_dir():
            continue
        parts = dist_info.name.removesuffix(".dist-info").rsplit("-", 1)
        if len(parts) != 2:
            continue
        name = _normalise(parts[0])
        version = parts[1]
        direct_url = dist_info / "direct_url.json"
        commit = ""
        if direct_url.is_file():
            try:
                data = json.loads(direct_url.read_text(encoding="utf-8"))
                commit = str((data.get("vcs_info") or {}).get("commit_id") or "")
            except (OSError, ValueError):
                commit = ""
        if commit:
            installed_git[name] = commit
        else:
            installed[name] = version
    return installed, installed_git


def venv_build_lock_fact(
    venv: Path, site_packages: Path, declared_lock: Path
) -> ModelSurfaceFact | None:
    marker = venv / ".built-from"
    if not marker.is_file():
        return None
    fields: dict[str, str] = {}
    try:
        for line in marker.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            key, sep, value = line.partition("=")
            if sep:
                fields[key.strip()] = value.strip()
    except (OSError, UnicodeDecodeError):
        return ModelSurfaceUnhealthyFact(
            surface="venv:build-lock",
            reason=f"marker {marker} is not readable",
        )
    lock_sha256 = fields.get("lock_sha256", "")
    lock_copy = fields.get("lock_copy", "")
    if not lock_sha256 or not lock_copy:
        return ModelSurfaceUnhealthyFact(
            surface="venv:build-lock",
            reason=f"marker {marker} is missing lock_sha256 or lock_copy",
        )
    if "/" in lock_copy or lock_copy in ("", ".", ".."):
        return ModelSurfaceUnhealthyFact(
            surface="venv:build-lock",
            reason=f"marker {marker} names lock_copy {lock_copy!r}, not a bare file name",
        )
    copy = venv / lock_copy
    try:
        raw = copy.read_bytes()
    except OSError:
        return ModelSurfaceUnhealthyFact(
            surface="venv:build-lock",
            reason=f"build lock copy {copy} is unreadable",
        )
    actual = hashlib.sha256(raw).hexdigest()
    if actual != lock_sha256:
        return ModelSurfaceUnhealthyFact(
            surface="venv:build-lock",
            reason=(
                f"build lock copy {copy} has sha256 {actual[:12]}, "
                f"but the marker names {lock_sha256[:12]}; the hash differs"
            ),
        )
    installed, installed_git = _scan_site_packages(site_packages)
    document = ModelVenvBuildLockDocument(
        checkout_lock_path=str(declared_lock),
        surfaces=(
            ModelVenvBuildLockSurface(
                label="venv:dispatch",
                build_lock_path=str(copy),
                installed=installed,
                installed_git=installed_git,
            ),
        ),
    )
    with tempfile.TemporaryDirectory(prefix="onex-reconcile-venv-lock-") as tmp:
        facts_path = Path(tmp) / "dispatch.venv-build-lock-facts.json"
        facts_path.write_text(document.model_dump_json(), encoding="utf-8")
        try:
            result = ValidatorVenvBuildLock().validate_file(facts_path)
        except ModelOnexError as error:
            return ModelSurfaceIndeterminateFact(
                surface="venv:build-lock",
                detail=f"the build-lock validator could not run: {error.message}",
            )
    issues = tuple(
        ModelVenvBuildLockIssue(
            code=issue.code or issue.rule_name or "unknown",
            severity=issue.severity.value
            if hasattr(issue.severity, "value")
            else str(issue.severity),
            message=issue.message,
        )
        for issue in result.issues
    )
    surfaces_judged = 0 if any(i.code == "build_lock_unreadable" for i in issues) else 1
    return ModelVenvBuildLockFact(
        venv=str(venv),
        build_lock_path=str(copy),
        lock_sha256=lock_sha256,
        surfaces_judged=surfaces_judged,
        valid=result.is_valid,
        issues=issues,
    )
