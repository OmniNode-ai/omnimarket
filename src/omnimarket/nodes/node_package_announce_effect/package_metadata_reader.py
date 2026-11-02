# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Reads the contracts an installed distribution lists in its own metadata (OMN-21597).

This module is the only place in the announcement step that names a metadata group.
The catalog and everything on the container side see announcements, never groups.

Nothing here imports a package: a contract is located from the entry-point value
(a dotted module path) by looking for ``contract.yaml`` beside that module on the
distribution's own file tree and on the search roots, so a package's optional
runtime dependencies need not be installed for it to announce.
"""

from collections.abc import Iterable, Sequence
from importlib.metadata import Distribution
from pathlib import Path

from omnibase_core.enums.enum_announced_node_kind import EnumAnnouncedNodeKind
from omnibase_core.models.events.contract_registration.model_announced_package import (
    ModelAnnouncedPackage,
)

from omnimarket.nodes.node_package_announce_effect.models.model_installed_contract import (
    ModelInstalledContract,
)

__all__ = ["read_installed_contracts"]

_GROUP_KINDS: dict[str, EnumAnnouncedNodeKind] = {
    "onex.nodes": EnumAnnouncedNodeKind.NODE,
    "onex.domain_plugins": EnumAnnouncedNodeKind.KERNEL_PLUGIN,
}
_CONTRACT_FILE = "contract.yaml"


def _normalized(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def _locate_contract(module: str, roots: Sequence[Path]) -> Path | None:
    relative = Path(*module.split("."))
    for root in roots:
        for directory in (relative, relative.parent):
            candidate = root / directory / _CONTRACT_FILE
            if candidate.is_file():
                return candidate
    return None


def read_installed_contracts(
    distributions: Iterable[Distribution], search_roots: Sequence[Path]
) -> tuple[ModelInstalledContract, ...]:
    """List every contract the given distributions name in their own metadata.

    A distribution seen twice (two copies on the path) counts once, as the first
    copy, matching how metadata lookups resolve a name.
    """
    seen: set[str] = set()
    found: list[ModelInstalledContract] = []
    for dist in distributions:
        dist_name = dist.metadata["Name"]
        if not dist_name or _normalized(dist_name) in seen:
            continue
        seen.add(_normalized(dist_name))
        package = ModelAnnouncedPackage(distribution=dist_name, version=dist.version)
        roots = (Path(str(dist.locate_file(""))), *search_roots)
        for entry_point in dist.entry_points:
            kind = _GROUP_KINDS.get(entry_point.group)
            if kind is None:
                continue
            module = entry_point.value.split(":")[0].split("[")[0].strip()
            found.append(
                ModelInstalledContract(
                    listing_name=entry_point.name,
                    package=package,
                    node_kind=kind,
                    contract_path=_locate_contract(module, roots),
                )
            )
    return tuple(found)
