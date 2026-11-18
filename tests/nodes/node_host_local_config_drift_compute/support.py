# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders for the drift read: a declaration and the measurement that agrees with it (OMN-24242).

Every name is synthetic: documentation addresses and example paths, never a real host, alias,
directory or key.
"""

from __future__ import annotations

from omnimarket.nodes.node_host_local_config_drift_compute.models.model_host_local_config_drift import (
    ModelDeclaredEnvDir,
    ModelDeclaredSshAlias,
    ModelLocalConfigDeclaration,
    ModelLocalConfigMeasured,
    ModelMeasuredEnvDir,
    ModelMeasuredSshAlias,
)

HOST = "host-a"


def declaration(
    max_backups: int = 2, **overrides: object
) -> ModelLocalConfigDeclaration:
    """One synthetic host's declared local configuration; ``overrides`` replace top-level fields.

    Two ssh aliases, one safe directory, one allowed repo-local git key and one env directory of
    live files ``.env`` holding at most ``max_backups`` other copies.
    """
    fields: dict[str, object] = {
        "host": HOST,
        "ssh_aliases": (
            ModelDeclaredSshAlias(alias="host-b", hostname="192.0.2.2"),
            ModelDeclaredSshAlias(alias="host-c", hostname="192.0.2.3"),
        ),
        "git_safe_directories": ("/srv/example/clone",),
        "git_allowed_local_keys": ("core.hookspath",),
        "env_dirs": (
            ModelDeclaredEnvDir(
                directory="~/.omnibase", live_files=(".env",), max_backups=max_backups
            ),
        ),
    }
    fields.update(overrides)
    return ModelLocalConfigDeclaration(**fields)


def measured_matching(
    declared: ModelLocalConfigDeclaration,
) -> ModelLocalConfigMeasured:
    """The snapshot of names measured on ``declared``'s host that agrees with it entirely."""
    return ModelLocalConfigMeasured(
        host=declared.host,
        ssh_aliases=tuple(
            ModelMeasuredSshAlias(alias=alias.alias, hostname=alias.hostname)
            for alias in declared.ssh_aliases
        ),
        git_safe_directories=declared.git_safe_directories,
        git_local_keys=declared.git_allowed_local_keys,
        env_dirs=tuple(
            ModelMeasuredEnvDir(directory=rule.directory, files=rule.live_files)
            for rule in declared.env_dirs
        ),
    )
