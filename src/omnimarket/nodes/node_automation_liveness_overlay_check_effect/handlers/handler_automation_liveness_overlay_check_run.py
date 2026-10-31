# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the overlay, the sources declaration and the registry clones, then run the check (OMN-20804).

The pure completeness check is handed text and judges it. This handler is the reading half: it
finds the private overlay and sources declaration, walks every registry repository the declaration
names, collects each candidate file and hands all of it to the pure handler.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from omnimarket.models.automation_liveness_overlay_check import (
    ModelAutomationLivenessOverlayCheckRequest,
    ModelAutomationLivenessScannedFile,
)
from omnimarket.nodes.node_automation_liveness_overlay_check_compute.handlers.handler_automation_liveness_installer_scan import (
    EXCLUDED_DIRECTORIES,
    is_candidate_path,
)
from omnimarket.nodes.node_automation_liveness_overlay_check_compute.handlers.handler_automation_liveness_overlay_check import (
    HandlerAutomationLivenessOverlayCheck,
)

from ..models import (
    ModelAutomationLivenessOverlayCheckRunRequest,
    ModelAutomationLivenessOverlayCheckRunResult,
)

#: The overlay and its sources declaration live in the private repository, under
#: ``src/<package>/overlays``; the package directory is found, never named here.
OVERLAY_FILENAME = "automation_liveness_overlay.yaml"
SOURCES_FILENAME = "automation_liveness_sources.yaml"
_MAX_FILE_BYTES = 1_000_000
_COMMAND = "onex-automation-liveness-check"


class HandlerAutomationLivenessOverlayCheckRun:
    """Check the private overlay against the installers of the registry clones."""

    def handle(
        self, request: ModelAutomationLivenessOverlayCheckRunRequest
    ) -> ModelAutomationLivenessOverlayCheckRunResult:
        internal = request.internal_root
        overlay_path = request.overlay or self.find_private_file(
            internal, OVERLAY_FILENAME
        )
        sources_path = request.sources or self.find_private_file(
            internal, SOURCES_FILENAME
        )
        for required in (overlay_path, sources_path):
            if not required.is_file():
                return ModelAutomationLivenessOverlayCheckRunResult(
                    exit_code=2,
                    stderr_lines=(f"{_COMMAND}: {required} is missing",),
                )
        sources_text = self._read_text(sources_path)
        repositories = self._declared_repositories(sources_text)
        private_repository = request.private_repository or internal.resolve().name

        registry_root = request.registry_root
        files: list[ModelAutomationLivenessScannedFile] = []
        scanned: list[str] = []
        for repository in repositories:
            if repository == private_repository:
                root: Path | None = internal
            elif request.internal_only or registry_root is None:
                root = None
            else:
                root = registry_root / repository
            if root is not None and root.is_dir():
                scanned.append(repository)
                files.extend(self.collect_files(repository, root))
        if not request.internal_only and registry_root is None:
            return ModelAutomationLivenessOverlayCheckRunResult(
                exit_code=2,
                stderr_lines=(
                    f"{_COMMAND}: set OMNI_HOME or pass --registry-root, "
                    "or pass --internal-only for a partial run",
                ),
            )

        result = HandlerAutomationLivenessOverlayCheck().handle(
            ModelAutomationLivenessOverlayCheckRequest(
                overlay_text=self._read_text(overlay_path),
                sources_text=sources_text,
                scanned_repositories=tuple(scanned),
                files=tuple(files),
                complete=not request.internal_only,
            )
        )
        scope = "complete" if result.complete else "PARTIAL (internal only)"
        lines = (
            *(finding.render() for finding in result.findings),
            f"automation-liveness overlay: {result.entries_checked} entries, "
            f"{result.sources_found} sources found in {len(result.repositories_scanned)} "
            f"repositories, {len(result.findings)} findings, scope {scope}",
        )
        return ModelAutomationLivenessOverlayCheckRunResult(
            exit_code=0 if result.passed else 1, stdout_lines=lines
        )

    @staticmethod
    def find_private_file(root: Path, filename: str) -> Path:
        """The ``src/*/overlays/<filename>`` file of the private checkout at ``root``."""
        matches = sorted(root.glob(f"src/*/overlays/{filename}"))
        return matches[0] if matches else root / "src" / "overlays" / filename

    @staticmethod
    def collect_files(
        repository: str, root: Path
    ) -> tuple[ModelAutomationLivenessScannedFile, ...]:
        """Every readable candidate file under ``root``, with paths relative to it."""
        files: list[ModelAutomationLivenessScannedFile] = []
        for directory, subdirectories, names in os.walk(root):
            # A nested git repository or worktree is another repository's files, not this one's.
            subdirectories[:] = sorted(
                d
                for d in subdirectories
                if d not in EXCLUDED_DIRECTORIES
                and not d.startswith(".")
                and not Path(directory, d, ".git").exists()
            )
            for name in sorted(names):
                path = Path(directory, name)
                relative = path.relative_to(root).as_posix()
                if not is_candidate_path(relative) or path.is_symlink():
                    continue
                try:
                    if path.stat().st_size > _MAX_FILE_BYTES:
                        continue
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                files.append(
                    ModelAutomationLivenessScannedFile(
                        repository=repository, path=relative, text=text
                    )
                )
        return tuple(files)

    @staticmethod
    def _read_text(path: Path) -> str:
        return path.read_text(encoding="utf-8")

    @staticmethod
    def _declared_repositories(sources_text: str) -> tuple[str, ...]:
        try:
            declared = yaml.safe_load(sources_text)
        except yaml.YAMLError:
            return ()
        names = declared.get("repositories") if isinstance(declared, dict) else None
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            return ()
        return tuple(names)
