# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The drift read answers an agreeing measurement working and names each difference (OMN-24242).

One test per rule: each plants exactly one difference in an otherwise agreeing measurement and
asserts the finding kind it must produce. The failing controls pin the reads the rules must not
lose: the wildcard safe directory, the declared backup bound and the host comparison.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_host_local_config_drift_compute.handlers.handler_host_local_config_drift import (
    HandlerHostLocalConfigDrift,
)
from omnimarket.nodes.node_host_local_config_drift_compute.models.model_host_local_config_drift import (
    EnumLocalConfigDriftKind,
    EnumLocalConfigVerdict,
    ModelLocalConfigDeclaration,
    ModelLocalConfigDriftCommand,
    ModelLocalConfigDriftResult,
    ModelLocalConfigMeasured,
    ModelMeasuredEnvDir,
    ModelMeasuredSshAlias,
)
from tests.nodes.node_host_local_config_drift_compute.support import (
    HOST,
    declaration,
    measured_matching,
)

pytestmark = pytest.mark.unit

_Kind = EnumLocalConfigDriftKind
_HANDLER = HandlerHostLocalConfigDrift()
_CORRELATION_ID = "drift-correlation-1"


def _run(
    declared: ModelLocalConfigDeclaration,
    measured: ModelLocalConfigMeasured,
) -> ModelLocalConfigDriftResult:
    return _HANDLER.handle(
        ModelLocalConfigDriftCommand(
            correlation_id=_CORRELATION_ID,
            declaration=declared,
            measured=measured,
        )
    )


def test_matching_names_read_working_without_findings() -> None:
    declared = declaration()

    result = _run(declared, measured_matching(declared))

    assert result.verdict is EnumLocalConfigVerdict.WORKING
    assert result.findings == ()
    assert result.host == HOST
    assert result.correlation_id == _CORRELATION_ID


def test_an_undeclared_ssh_alias_is_found() -> None:
    declared = declaration()
    measured = measured_matching(declared)

    result = _run(
        declared,
        measured.model_copy(
            update={
                "ssh_aliases": (
                    *measured.ssh_aliases,
                    ModelMeasuredSshAlias(alias="host-extra", hostname="192.0.2.9"),
                )
            }
        ),
    )

    assert result.verdict is EnumLocalConfigVerdict.NOT_WORKING
    assert [(f.kind, f.subject, f.detail) for f in result.findings] == [
        (_Kind.UNDECLARED_SSH_ALIAS, "host-extra", "measured hostname 192.0.2.9")
    ]


def test_a_missing_ssh_alias_is_found() -> None:
    declared = declaration()
    measured = measured_matching(declared)

    result = _run(
        declared,
        measured.model_copy(update={"ssh_aliases": measured.ssh_aliases[:-1]}),
    )

    assert [(f.kind, f.subject, f.detail) for f in result.findings] == [
        (_Kind.MISSING_SSH_ALIAS, "host-c", "declared hostname 192.0.2.3")
    ]


def test_an_ssh_alias_with_another_target_is_found() -> None:
    declared = declaration()
    measured = measured_matching(declared)

    result = _run(
        declared,
        measured.model_copy(
            update={
                "ssh_aliases": (
                    ModelMeasuredSshAlias(alias="host-b", hostname="192.0.2.99"),
                    *measured.ssh_aliases[1:],
                )
            }
        ),
    )

    assert [(f.kind, f.subject, f.detail) for f in result.findings] == [
        (
            _Kind.SSH_ALIAS_TARGET_DIFFERS,
            "host-b",
            "declared 192.0.2.2 measured 192.0.2.99",
        )
    ]


def test_an_undeclared_safe_directory_is_found() -> None:
    declared = declaration()
    measured = measured_matching(declared)

    result = _run(
        declared,
        measured.model_copy(
            update={
                "git_safe_directories": (
                    *measured.git_safe_directories,
                    "/srv/other/clone",
                )
            }
        ),
    )

    assert [(f.kind, f.subject, f.detail) for f in result.findings] == [
        (
            _Kind.UNDECLARED_SAFE_DIRECTORY,
            "/srv/other/clone",
            "measured but not declared",
        )
    ]


def test_an_undeclared_local_git_key_is_found() -> None:
    declared = declaration()
    measured = measured_matching(declared)

    result = _run(
        declared,
        measured.model_copy(
            update={"git_local_keys": (*measured.git_local_keys, "core.editor")}
        ),
    )

    assert [(f.kind, f.subject, f.detail) for f in result.findings] == [
        (_Kind.UNDECLARED_LOCAL_GIT_KEY, "core.editor", "measured but not declared")
    ]


def test_env_backups_over_the_bound_are_found() -> None:
    declared = declaration()
    measured = measured_matching(declared)

    result = _run(
        declared,
        measured.model_copy(
            update={
                "env_dirs": (
                    ModelMeasuredEnvDir(
                        directory="~/.omnibase",
                        files=(".env", ".env.1", ".env.2", ".env.3"),
                    ),
                )
            }
        ),
    )

    assert [(f.kind, f.subject, f.detail) for f in result.findings] == [
        (_Kind.ENV_BACKUPS_OVER_LIMIT, "~/.omnibase", "3 backups, limit 2")
    ]


def test_a_missing_live_env_file_is_found() -> None:
    declared = declaration()
    measured = measured_matching(declared)

    result = _run(
        declared,
        measured.model_copy(
            update={
                "env_dirs": (
                    ModelMeasuredEnvDir(directory="~/.omnibase", files=(".env.old",)),
                )
            }
        ),
    )

    assert [(f.kind, f.subject, f.detail) for f in result.findings] == [
        (_Kind.MISSING_ENV_LIVE_FILE, ".env", "declared live file of ~/.omnibase")
    ]


def test_an_undeclared_env_dir_is_found() -> None:
    declared = declaration()
    measured = measured_matching(declared)

    result = _run(
        declared,
        measured.model_copy(
            update={
                "env_dirs": (
                    *measured.env_dirs,
                    ModelMeasuredEnvDir(directory="~/elsewhere", files=(".env",)),
                )
            }
        ),
    )

    assert [(f.kind, f.subject, f.detail) for f in result.findings] == [
        (_Kind.UNDECLARED_ENV_DIR, "~/elsewhere", "measured but not declared")
    ]


def test_control_the_wildcard_safe_directory_is_not_waved_through() -> None:
    """Failing control: the break where a measured "*" is read as a declared entry."""
    declared = declaration()
    measured = measured_matching(declared)

    result = _run(
        declared,
        measured.model_copy(
            update={"git_safe_directories": (*measured.git_safe_directories, "*")}
        ),
    )

    assert [(f.kind, f.subject) for f in result.findings] == [
        (_Kind.UNDECLARED_SAFE_DIRECTORY, "*")
    ]


def test_control_the_backup_bound_follows_the_declaration() -> None:
    """Failing control, the break named: raising the bound.

    Five backup copies break a bound of two; the same snapshot against a declaration whose bound
    is five reads working, so the read enforces the declared bound and no number of its own.
    """
    measured = measured_matching(declaration()).model_copy(
        update={
            "env_dirs": (
                ModelMeasuredEnvDir(
                    directory="~/.omnibase",
                    files=(".env", ".env.1", ".env.2", ".env.3", ".env.4", ".env.5"),
                ),
            )
        }
    )

    over = _run(declaration(), measured)
    assert [(f.kind, f.subject, f.detail) for f in over.findings] == [
        (_Kind.ENV_BACKUPS_OVER_LIMIT, "~/.omnibase", "5 backups, limit 2")
    ]

    raised = _run(declaration(max_backups=5), measured)
    assert raised.verdict is EnumLocalConfigVerdict.WORKING
    assert raised.findings == ()


def test_control_a_measurement_of_another_host_is_refused() -> None:
    """Failing control: the break where the two host names are never compared, which would read
    one host's measurement against another host's declaration."""
    declared = declaration()
    measured = measured_matching(declared).model_copy(update={"host": "host-elsewhere"})

    with pytest.raises(
        ValueError, match="measurement host does not match the declaration host"
    ):
        _run(declared, measured)


def test_findings_are_ordered_by_kind_then_subject() -> None:
    """One measurement holding all eight differences names them in the same order every run."""
    declared = declaration()
    measured = measured_matching(declared)

    result = _run(
        declared,
        measured.model_copy(
            update={
                "ssh_aliases": (
                    ModelMeasuredSshAlias(alias="host-b", hostname="192.0.2.99"),
                    ModelMeasuredSshAlias(alias="host-extra", hostname="192.0.2.9"),
                ),
                "git_safe_directories": (*measured.git_safe_directories, "*"),
                "git_local_keys": (*measured.git_local_keys, "core.editor"),
                "env_dirs": (
                    ModelMeasuredEnvDir(
                        directory="~/.omnibase",
                        files=(".env.1", ".env.2", ".env.3", ".env.4"),
                    ),
                    ModelMeasuredEnvDir(directory="~/elsewhere", files=(".env",)),
                ),
            }
        ),
    )

    assert result.verdict is EnumLocalConfigVerdict.NOT_WORKING
    assert [(f.kind, f.subject) for f in result.findings] == [
        (_Kind.ENV_BACKUPS_OVER_LIMIT, "~/.omnibase"),
        (_Kind.MISSING_ENV_LIVE_FILE, ".env"),
        (_Kind.MISSING_SSH_ALIAS, "host-c"),
        (_Kind.SSH_ALIAS_TARGET_DIFFERS, "host-b"),
        (_Kind.UNDECLARED_ENV_DIR, "~/elsewhere"),
        (_Kind.UNDECLARED_LOCAL_GIT_KEY, "core.editor"),
        (_Kind.UNDECLARED_SAFE_DIRECTORY, "*"),
        (_Kind.UNDECLARED_SSH_ALIAS, "host-extra"),
    ]
