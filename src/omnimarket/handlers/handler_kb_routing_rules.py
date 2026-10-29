# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Load the knowledge-store routing rules: contract default plus a deep-merged overlay.

The default path and the overlay path are injected, so the same code boots on
the default data and on a real overlay with no code change. The shape follows
the delegation routing reducer: rules in a contract file, an overlay deep-merged
over the default.
"""

from __future__ import annotations

import fnmatch
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from omnimarket.models.kb_routing.model_kb_manifest import (
    ModelKbManifest,
    ModelKbManifestFile,
)
from omnimarket.models.kb_routing.model_kb_routing_error import KbRoutingRulesError
from omnimarket.models.kb_routing.model_kb_routing_rules import ModelKbRoutingRules

#: Default rules, shipped with the package.
DEFAULT_RULES_PATH = (
    Path(__file__).resolve().parent.parent
    / "contracts"
    / "kb_routing"
    / "kb_routing_rules.v1.yaml"
)
#: Environment variables that name the overlay and the manifest of a real site.
OVERLAY_PATH_ENV = "KB_ROUTING_OVERLAY_PATH"
MANIFEST_PATH_ENV = "KB_ROUTING_MANIFEST_PATH"


def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Overlay wins; mappings merge key by key; lists and scalars are replaced."""
    merged: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def read_yaml_mapping(path: Path) -> dict[str, Any]:
    """Read a YAML file that must hold a mapping."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise KbRoutingRulesError(f"{path.name} does not hold a mapping")
    return data


def resolve_manifest(
    rules: ModelKbRoutingRules, manifest: ModelKbManifest
) -> dict[str, str]:
    """Map each manifest file name to exactly one folder key, or fail naming the file."""
    assignments: dict[str, str] = {}
    for file in manifest.files:
        matches = [
            folder.key
            for folder in rules.drive_folders.values()
            if _folder_matches(folder.selectors, file)
        ]
        if len(matches) != 1:
            raise KbRoutingRulesError(
                f"file {file.name!r} resolves to {len(matches)} folder keys "
                f"{sorted(matches)}, expected exactly one"
            )
        assignments[file.name] = matches[0]
    return assignments


def _folder_matches(selectors: Any, file: ModelKbManifestFile) -> bool:
    return any(
        s.group == file.group
        and (s.name_glob is None or fnmatch.fnmatchcase(file.name, s.name_glob))
        for s in selectors
    )


def verify_counts(assignments: Mapping[str, str], manifest: ModelKbManifest) -> None:
    """Fail unless the per-folder-key counts equal the manifest's table."""
    actual = dict(Counter(assignments.values()))
    expected = {k: v for k, v in manifest.expected_counts.items() if v}
    if actual != expected:
        raise KbRoutingRulesError(
            f"per-folder counts {sorted(actual.items())} differ from the table "
            f"{sorted(expected.items())}"
        )


class ModelKbRealOverlayReport(BaseModel):
    """The outcome of the real-overlay pass: ran with counts, or not run and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ran: bool
    detail: str
    counts: dict[str, int] = {}


class HandlerKbRoutingRules:
    """Loads and validates the rules; paths are injected by the caller."""

    def __init__(
        self,
        default_path: Path = DEFAULT_RULES_PATH,
        overlay_path: Path | None = None,
    ) -> None:
        self._default_path = default_path
        self._overlay_path = overlay_path

    def load(self, manifest: ModelKbManifest | None = None) -> ModelKbRoutingRules:
        """Merge the overlay over the default and validate; fail the load on any error."""
        data = read_yaml_mapping(self._default_path)
        if self._overlay_path is not None:
            data = deep_merge(data, read_yaml_mapping(self._overlay_path))
        try:
            rules = ModelKbRoutingRules.model_validate(data)
        except ValidationError as exc:
            raise KbRoutingRulesError(str(exc)) from exc
        if manifest is not None:
            verify_counts(resolve_manifest(rules, manifest), manifest)
        return rules

    @staticmethod
    def load_manifest(path: Path) -> ModelKbManifest:
        """Read a manifest file."""
        try:
            return ModelKbManifest.model_validate(read_yaml_mapping(path))
        except ValidationError as exc:
            raise KbRoutingRulesError(str(exc)) from exc

    @classmethod
    def run_real_overlay_pass(
        cls, environ: Mapping[str, str]
    ) -> ModelKbRealOverlayReport:
        """Run the loader on the real overlay and manifest the environment names.

        When either variable is absent the pass is not run and says so; it never
        reports a pass it did not run.
        """
        overlay = environ.get(OVERLAY_PATH_ENV)
        manifest = environ.get(MANIFEST_PATH_ENV)
        if not overlay or not manifest:
            return ModelKbRealOverlayReport(
                ran=False,
                detail=(
                    f"not run: {OVERLAY_PATH_ENV} and {MANIFEST_PATH_ENV} "
                    "are not both set"
                ),
            )
        loaded = cls.load_manifest(Path(manifest))
        rules = cls(overlay_path=Path(overlay)).load(loaded)
        counts = dict(Counter(resolve_manifest(rules, loaded).values()))
        return ModelKbRealOverlayReport(
            ran=True, detail="ran: overlay and manifest loaded", counts=counts
        )


__all__ = [
    "DEFAULT_RULES_PATH",
    "MANIFEST_PATH_ENV",
    "OVERLAY_PATH_ENV",
    "HandlerKbRoutingRules",
    "ModelKbRealOverlayReport",
    "deep_merge",
    "read_yaml_mapping",
    "resolve_manifest",
    "verify_counts",
]
