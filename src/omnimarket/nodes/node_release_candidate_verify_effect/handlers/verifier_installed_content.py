# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The installed-content check: RECORD files, protected entry points and the generated launcher.

Reads one installed environment and compares it with the candidate manifest in three parts:
(a) every file the manifest's RECORD lists is present with its sha256 (RECORD itself excepted),
(b) the protected entry-point groups equal the manifest's union and each protected console script
has exactly its manifest owner and target, (c) the launcher resolves into this environment and its
whole text equals an admitted installer template rebuilt with this environment's interpreter and the
manifest's target.
"""

from __future__ import annotations

import difflib
import hashlib
import importlib.metadata as importlib_metadata
import re
from pathlib import Path

from omnimarket.nodes.node_release_candidate_verify_effect.models.enum_release_candidate_finding import (
    EnumReleaseCandidateFinding as K,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_manifest import (
    ModelReleaseCandidateManifest,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_overlay import (
    ModelLauncherTemplate,
    ModelReleaseCandidateOverlay,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_verify_result import (
    ModelInstalledDistribution,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_verify_result import (
    ModelReleaseCandidateFinding as Finding,
)


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _site_packages(prefix: Path) -> Path | None:
    found = sorted(prefix.glob("lib/python*/site-packages"))
    return found[0] if len(found) == 1 else None


def _layout(prefix: Path) -> str:
    if (prefix / "uv-receipt.toml").is_file():
        return "uv_tool"
    if (prefix / "pipx_metadata.json").is_file():
        return "pipx"
    return "venv"


def _check_record_files(
    manifest: ModelReleaseCandidateManifest,
    site: Path,
    installed: dict[str, importlib_metadata.Distribution],
    findings: list[Finding],
) -> None:
    for dist in manifest.distributions:
        found = installed.get(normalize(dist.name))
        if found is None:
            findings.append(Finding(kind=K.DISTRIBUTION_MISSING, subject=dist.name))
            continue
        if found.version != dist.version:
            findings.append(
                Finding(
                    kind=K.DISTRIBUTION_VERSION,
                    subject=dist.name,
                    detail=f"installed {found.version}, manifest {dist.version}",
                )
            )
        for entry in dist.record:
            if entry.path.startswith("..") or not entry.sha256:
                continue  # scripts the installer writes, and lines that list no hash
            if entry.path.endswith(".dist-info/RECORD"):
                continue  # RECORD cannot list its own hash
            target = site / entry.path
            if not target.is_file():
                findings.append(Finding(kind=K.FILE_MISSING, subject=entry.path))
            elif sha256_file(target) != entry.sha256:
                findings.append(Finding(kind=K.FILE_MODIFIED, subject=entry.path))


def _check_entry_points(
    manifest: ModelReleaseCandidateManifest,
    overlay: ModelReleaseCandidateOverlay,
    installed: dict[str, importlib_metadata.Distribution],
    findings: list[Finding],
) -> None:
    for group in overlay.protected_groups:
        expected: dict[str, str] = {}
        for published in manifest.distributions:
            expected.update(published.entry_points.get(group, {}))
        actual: dict[str, tuple[str, str]] = {}
        for dist_name, dist in installed.items():
            for point in dist.entry_points:
                if point.group != group:
                    continue
                if point.name in actual and actual[point.name][1] != dist_name:
                    findings.append(
                        Finding(
                            kind=K.ENTRY_POINT_EXTRA,
                            subject=f"{group}:{point.name}",
                            detail=f"declared again by {dist_name}",
                        )
                    )
                actual[point.name] = (point.value, dist_name)
        for name, (value, owner) in sorted(actual.items()):
            if name not in expected:
                findings.append(
                    Finding(
                        kind=K.ENTRY_POINT_EXTRA,
                        subject=f"{group}:{name}",
                        detail=f"{value} declared by {owner}",
                    )
                )
            elif expected[name] != value:
                findings.append(
                    Finding(
                        kind=K.ENTRY_POINT_REDIRECTED,
                        subject=f"{group}:{name}",
                        detail=f"{value} declared by {owner}, manifest {expected[name]}",
                    )
                )
        for name in sorted(set(expected) - set(actual)):
            findings.append(
                Finding(kind=K.ENTRY_POINT_MISSING, subject=f"{group}:{name}")
            )
    for script in manifest.protected_scripts:
        owners = {
            dist_name: point.value
            for dist_name, dist in installed.items()
            for point in dist.entry_points
            if point.group == "console_scripts" and point.name == script.name
        }
        manifest_owner = normalize(script.owner)
        for owner, value in sorted(owners.items()):
            if owner != manifest_owner:
                findings.append(
                    Finding(
                        kind=K.PROTECTED_SCRIPT_OWNER,
                        subject=script.name,
                        detail=f"declared by {owner}, manifest owner {manifest_owner}",
                    )
                )
            elif value != script.target:
                findings.append(
                    Finding(
                        kind=K.PROTECTED_SCRIPT_TARGET,
                        subject=script.name,
                        detail=f"{value}, manifest {script.target}",
                    )
                )
        if manifest_owner not in owners:
            findings.append(
                Finding(
                    kind=K.PROTECTED_SCRIPT_OWNER,
                    subject=script.name,
                    detail=f"manifest owner {manifest_owner} does not declare it",
                )
            )


def _render(template: ModelLauncherTemplate, interpreter: str, target: str) -> str:
    module, _, attr = target.partition(":")
    return (
        template.text.replace("{interpreter}", interpreter)
        .replace("{target_module}", module)
        .replace("{target_attr}", attr)
    )


def _first_difference(expected: str, actual: str) -> tuple[int, str, str]:
    exp_lines, act_lines = expected.splitlines(), actual.splitlines()
    for index in range(max(len(exp_lines), len(act_lines))):
        exp = exp_lines[index] if index < len(exp_lines) else "<end of file>"
        act = act_lines[index] if index < len(act_lines) else "<end of file>"
        if exp != act:
            return index + 1, exp, act
    return 0, "", ""


def _check_launcher(
    manifest: ModelReleaseCandidateManifest,
    overlay: ModelReleaseCandidateOverlay,
    prefix: Path,
    onex_on_path: str | None,
    findings: list[Finding],
) -> None:
    name = overlay.launcher_script_name
    script = next((s for s in manifest.protected_scripts if s.name == name), None)
    if script is None:
        findings.append(
            Finding(
                kind=K.LAUNCHER_DIFFERS,
                subject=name,
                detail="manifest lists no such script",
            )
        )
        return
    launcher = prefix / "bin" / name
    if not onex_on_path or Path(onex_on_path).resolve() != launcher.resolve():
        findings.append(
            Finding(
                kind=K.LAUNCHER_OUTSIDE_ENVIRONMENT,
                subject=name,
                detail=f"command -v {name} gave {onex_on_path or 'nothing'}, environment launcher {launcher}",
            )
        )
        return
    if not (prefix / "pyvenv.cfg").is_file():
        findings.append(
            Finding(
                kind=K.LAUNCHER_LAYOUT,
                subject=str(prefix),
                detail="not a uv tool, pipx or venv environment",
            )
        )
        return
    actual = launcher.read_text(encoding="utf-8")
    interpreters = {str(prefix), str(prefix.resolve())}
    best: tuple[float, int, str, str, str] | None = None
    for template in overlay.launcher_templates:
        for interpreter_name in template.interpreter_names:
            for root in sorted(interpreters):
                expected = _render(
                    template, f"{root}/bin/{interpreter_name}", script.target
                )
                if expected == actual:
                    return
                closeness = difflib.SequenceMatcher(None, expected, actual).ratio()
                if best is None or closeness > best[0]:
                    best = (
                        closeness,
                        *_first_difference(expected, actual),
                        template.name,
                    )
    assert best is not None
    findings.append(
        Finding(
            kind=K.LAUNCHER_DIFFERS,
            subject=f"{launcher} line {best[1]}",
            detail=f"expected {best[2]!r} got {best[3]!r} (closest template {best[4]})",
        )
    )


def verify_installed(
    manifest: ModelReleaseCandidateManifest,
    overlay: ModelReleaseCandidateOverlay,
    environment_prefix: str,
    onex_on_path: str | None,
) -> tuple[list[Finding], str, tuple[ModelInstalledDistribution, ...]]:
    prefix = Path(environment_prefix)
    site = _site_packages(prefix)
    if site is None:
        return (
            [
                Finding(
                    kind=K.ENVIRONMENT_UNREADABLE,
                    subject=environment_prefix,
                    detail="no single lib/python*/site-packages",
                )
            ],
            "",
            (),
        )
    installed = {
        normalize(dist.metadata["Name"]): dist
        for dist in importlib_metadata.distributions(path=[str(site)])
    }
    findings: list[Finding] = []
    _check_record_files(manifest, site, installed, findings)
    _check_entry_points(manifest, overlay, installed, findings)
    _check_launcher(manifest, overlay, prefix, onex_on_path, findings)
    closure = tuple(
        ModelInstalledDistribution(name=name, version=dist.version)
        for name, dist in sorted(installed.items())
    )
    return findings, _layout(prefix), closure
