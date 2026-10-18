# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Find every file that puts an automatic process on a host (OMN-20804, plan 4.11).

The scan is pure: it reads the text it is handed. A file is a source when it is

* a systemd ``.service`` or ``.timer`` file,
* a launchd property list or a template of one (``.plist``, ``.plist.tmpl``, ``.plist.template``),
* any file inside a ``cron.d`` directory,
* a drift manifest (``host_drift_manifest.yaml``, ``unit-drift-manifest.yaml``), which names the
  units a host is expected to run, each of which needs an overlay entry, or
* code that writes a launchd agent, a systemd unit, a crontab or a cron.d file: a launchd
  bootstrap or load, a systemd link or daemon reload, a crontab installed from a pipe or a file,
  or a write to one of the unit directories.

It does not look at where a file lives, so an installer in a directory nobody listed is found.
Only what cannot install anything is left out: documentation, tests, fixtures, caches and hidden
directories, and templates of code (which an installer renders).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import PurePosixPath

import yaml

from omnimarket.nodes.node_automation_liveness_overlay_check_compute.models import (
    EnumAutomationLivenessSourceClass,
    ModelAutomationLivenessFoundSource,
    ModelAutomationLivenessInstallerScanRequest,
    ModelAutomationLivenessInstallerScanResult,
    ModelAutomationLivenessUnitReference,
)

#: Directories that hold no installer: caches, vendored code, documentation and test material.
EXCLUDED_DIRECTORIES: frozenset[str] = frozenset(
    {
        "node_modules",
        "venv",
        "__pycache__",
        "omni_worktrees",
        "tests",
        "test",
        "docs",
        "fixtures",
        "build",
        "dist",
        "site-packages",
    }
)

#: Label prefixes our launchd jobs have carried; a template's file name may use another one
#: than the label it renders.
LAUNCHD_LABEL_PREFIXES: tuple[str, ...] = ("ai.omninode.", "com.omninode.", "com.onex.")

_CODE_SUFFIXES = frozenset(
    {".sh", ".bash", ".zsh", ".py", ".js", ".mjs", ".cjs", ".ts"}
)
_PLIST_NAME = re.compile(r"^(?P<label>.+?)\.plist(?:\.tmpl|\.template|\.in)?$")

# The patterns below are assembled from parts so that this module does not read as an installer
# of itself: a literal unit directory next to a write verb is exactly what the scan looks for.
_UNIT_DIRECTORIES = re.compile(
    "|".join(
        [
            "/etc/" + "systemd/system",
            r"\.config/" + "systemd/" + "user",
            "systemd/" + "user",
            "/etc/" + r"cron\.d",
            "Library/" + "LaunchAgents",
        ]
    )
)
_LOAD_OR_LINK = re.compile(
    "|".join(
        [
            r"\blaunch" + r"ctl\s+(?:bootstrap|load)\b",
            r"""["']launch""" + r"""ctl["']\s*,\s*["'](?:bootstrap|load)["']""",
            r"\bsystem" + r"ctl\s+(?:--user\s+|--system\s+)?(?:link|daemon-reload)\b",
            r"""["']system"""
            + r"""ctl["']\s*,\s*(?:["']--(?:user|system)["']\s*,\s*)?"""
            r"""["'](?:link|daemon-reload)["']""",
            r"\bcron" + r"tab\s+(?:-u\s+\S+\s+)?(?:-(?=\s|$)|[/.\"'$~])",
            r"""["']cron""" + r"""tab["']\s*,\s*(?!["']-[lre]["'])""",
        ]
    )
)
_FILE_WRITE = re.compile(
    r"\bcp\s|\binstall\s+-|\btee\b|\bmv\s|write_"
    + r"text|write_"
    + r"bytes|copy"
    + r"file|"
    r"shutil\.copy"
)
# A shell redirect writes a file; in other languages the same characters are a type arrow or a
# comparison, so only a shell script is read for them.
_SHELL_REDIRECT = re.compile(r"(?<![-=])>>?\s*(?!/dev/null|&)[\"'$/~]")
_SHELL_SUFFIXES = frozenset({".sh", ".bash", ".zsh", ""})


def _is_comment(line: str) -> bool:
    stripped = line.lstrip()
    return stripped.startswith(("#", "//"))


def _path_parts(path: str) -> tuple[str, ...]:
    return PurePosixPath(path).parts


def _excluded(path: str) -> bool:
    parts = _path_parts(path)
    return any(
        part in EXCLUDED_DIRECTORIES or part.startswith(".") for part in parts[:-1]
    ) or parts[-1].startswith("test_")


def _writes_units(name: str, text: str) -> bool:
    code = "\n".join(line for line in text.splitlines() if not _is_comment(line))
    if _LOAD_OR_LINK.search(code):
        return True
    if not _UNIT_DIRECTORIES.search(code):
        return False
    if _FILE_WRITE.search(code):
        return True
    return PurePosixPath(name).suffix in _SHELL_SUFFIXES and bool(
        _SHELL_REDIRECT.search(code)
    )


def _is_code(name: str, text: str) -> bool:
    suffix = PurePosixPath(name).suffix
    return suffix in _CODE_SUFFIXES or (suffix == "" and text.startswith("#!"))


def is_candidate_path(path: str) -> bool:
    """True when the scan could classify this path, so a caller reads only these files."""
    if _excluded(path):
        return False
    name = _path_parts(path)[-1]
    suffix = PurePosixPath(name).suffix
    return (
        name in _MANIFEST_READERS
        or name.endswith((".service", ".timer"))
        or _PLIST_NAME.match(name) is not None
        or "cron.d" in _path_parts(path)[:-1]
        or suffix in _CODE_SUFFIXES
        or suffix == ""
    )


def _launchd_ids(label: str) -> tuple[str, ...]:
    for prefix in LAUNCHD_LABEL_PREFIXES:
        if label.startswith(prefix):
            tail = label[len(prefix) :]
            return tuple(f"{other}{tail}" for other in LAUNCHD_LABEL_PREFIXES)
    return (label,)


def _unit_alternatives(name: str) -> tuple[str, ...]:
    if name.endswith(".plist"):
        return _launchd_ids(name.removesuffix(".plist"))
    if name.endswith((".service", ".timer")):
        stem = name.rsplit(".", 1)[0]
        return (name, f"{stem}.timer", f"{stem}.service")
    return _launchd_ids(name)


def _host_drift_units(text: str) -> tuple[ModelAutomationLivenessUnitReference, ...]:
    """Units of every role in the host-drift manifest; a bare systemd name is a timer or service."""
    data = yaml.safe_load(text)
    roles = data.get("roles", {}) if isinstance(data, dict) else {}
    seen: dict[str, ModelAutomationLivenessUnitReference] = {}
    for role in roles.values():
        for unit in role.get("units", []) if isinstance(role, dict) else []:
            name = str(unit["name"])
            kind = str(unit.get("kind", ""))
            alternatives = (
                (f"{name}.timer", f"{name}.service")
                if kind.startswith("systemd")
                else _launchd_ids(name)
            )
            seen.setdefault(
                name,
                ModelAutomationLivenessUnitReference(
                    name=name, alternatives=alternatives
                ),
            )
    return tuple(seen.values())


def _unit_drift_units(text: str) -> tuple[ModelAutomationLivenessUnitReference, ...]:
    """Units of the deployed-unit drift manifest; its script entries are not units."""
    data = yaml.safe_load(text)
    units = data.get("units", []) if isinstance(data, dict) else []
    return tuple(
        ModelAutomationLivenessUnitReference(
            name=str(unit["name"]), alternatives=_unit_alternatives(str(unit["name"]))
        )
        for unit in units
        if str(unit["name"]).endswith((".service", ".timer", ".plist"))
    )


#: Manifests by file name: the units they name each need an overlay entry (plan 4.11).
_MANIFEST_READERS: dict[
    str, Callable[[str], tuple[ModelAutomationLivenessUnitReference, ...]]
] = {
    "host_drift_manifest.yaml": _host_drift_units,
    "unit-drift-manifest.yaml": _unit_drift_units,
}


def classify_source(
    repository: str, path: str, text: str
) -> ModelAutomationLivenessFoundSource | None:
    """The source a file is, or None when it installs nothing."""
    if _excluded(path):
        return None
    parts = _path_parts(path)
    name = parts[-1]
    reader = _MANIFEST_READERS.get(name)
    if reader is not None:
        try:
            required = reader(text)
        except (yaml.YAMLError, KeyError, TypeError, AttributeError):
            required = ()
        return ModelAutomationLivenessFoundSource(
            repository=repository,
            path=path,
            source_class=EnumAutomationLivenessSourceClass.UNIT_MANIFEST,
            required_units=required,
        )
    if name.endswith((".service", ".timer")):
        stem = name.rsplit(".", 1)[0]
        return ModelAutomationLivenessFoundSource(
            repository=repository,
            path=path,
            source_class=EnumAutomationLivenessSourceClass.SYSTEMD_UNIT_FILE,
            implied_native_ids=(f"{stem}.timer", f"{stem}.service"),
        )
    plist = _PLIST_NAME.match(name)
    if plist is not None:
        return ModelAutomationLivenessFoundSource(
            repository=repository,
            path=path,
            source_class=EnumAutomationLivenessSourceClass.LAUNCHD_PLIST,
            implied_native_ids=_launchd_ids(plist.group("label")),
        )
    if "cron.d" in parts[:-1]:
        return ModelAutomationLivenessFoundSource(
            repository=repository,
            path=path,
            source_class=EnumAutomationLivenessSourceClass.CRON_D_FILE,
            implied_native_ids=(f"cron.d:{name}",),
        )
    if _is_code(name, text) and _writes_units(name, text):
        return ModelAutomationLivenessFoundSource(
            repository=repository,
            path=path,
            source_class=EnumAutomationLivenessSourceClass.INSTALLER_CODE,
        )
    return None


class HandlerAutomationLivenessInstallerScan:
    """Scan supplied files for units, templates, cron files and installer code."""

    def handle(
        self, request: ModelAutomationLivenessInstallerScanRequest
    ) -> ModelAutomationLivenessInstallerScanResult:
        found = [
            source
            for file in request.files
            if (source := classify_source(file.repository, file.path, file.text))
            is not None
        ]
        found.sort(key=lambda source: (source.repository, source.path))
        return ModelAutomationLivenessInstallerScanResult(sources=tuple(found))
