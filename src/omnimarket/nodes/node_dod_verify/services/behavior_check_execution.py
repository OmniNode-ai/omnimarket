# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Environment and diagnostics for behavior checks executed by the verifier."""

from __future__ import annotations

import re
from collections.abc import Mapping

from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    ModelCommandCheckFailure,
)

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def scrub_database_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Tests provision their database; the caller cannot select it implicitly.

    Include libpq's PG* selectors and database URL aliases as well as the
    POSTGRES_* and DSN variables that caused the original host-dependent proof.
    Copy rather than mutating the verifier's own service configuration.
    """
    return {
        key: value
        for key, value in environment.items()
        if not (
            key.upper().startswith(("POSTGRES_", "PG"))
            or "DSN" in key.upper()
            or key.upper() in {"DATABASE_URL", "DB_URL"}
            or key.upper().endswith(("_DATABASE_URL", "_DB_URL"))
        )
    }


def command_failure(
    exit_code: int, stdout: str, stderr: str
) -> ModelCommandCheckFailure:
    """Read pytest's short summary and first error from BOTH output streams.

    These are diagnostics only: the exit code always decides failure, even
    when a runner emits no recognizable test summary or a forged banner.
    """
    lines = _ANSI_ESCAPE.sub("", stdout + "\n" + stderr).splitlines()
    test_ids: list[str] = []
    first_error: str | None = None
    for line in lines:
        match = re.match(r"^(?:FAILED|ERROR)\s+(\S+)", line.strip())
        if match and match.group(1) not in test_ids:
            test_ids.append(match.group(1))
        error = re.match(r"^E\s+(.+)", line)
        if first_error is None and error:
            first_error = error.group(1).strip()
    if first_error is None:
        first_error = next(
            (line.strip() for line in stderr.splitlines() if line.strip()), None
        )
    return ModelCommandCheckFailure(
        exit_code=exit_code,
        failing_test_ids=tuple(test_ids),
        first_error_line=first_error,
    )
