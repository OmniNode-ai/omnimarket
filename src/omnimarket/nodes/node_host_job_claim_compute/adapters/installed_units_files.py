# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The installed units as unit files on this host."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_host_job_claim_compute.models import ModelInstalledUnit


class InstalledUnitsFiles:
    """Read the unit files directly inside each declared directory."""

    def installed(
        self,
        unit_dirs: tuple[str, ...],
        suffix: str,
    ) -> tuple[ModelInstalledUnit, ...]:
        seen: dict[str, ModelInstalledUnit] = {}
        for raw in unit_dirs:
            where = Path(raw).expanduser()
            if not where.is_dir():
                continue
            for entry in sorted(where.iterdir()):
                if not entry.is_file() or not entry.name.endswith(suffix):
                    continue
                name = entry.name[: -len(suffix)]
                if not name or name in seen:
                    continue
                command = " ".join(
                    line.split("=", 1)[1].strip()
                    for line in entry.read_text(encoding="utf-8").splitlines()
                    if line.startswith("ExecStart=")
                )
                seen[name] = ModelInstalledUnit(name=name, command=command)
        return tuple(seen[name] for name in sorted(seen))


__all__ = ["InstalledUnitsFiles"]
