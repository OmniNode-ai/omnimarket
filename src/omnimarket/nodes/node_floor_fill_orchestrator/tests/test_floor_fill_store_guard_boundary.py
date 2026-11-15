# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23002: the store's own psycopg2 connection is a declared boundary for the freestanding imperative-IO guard.

The store opens its own ``psycopg2.connect`` (``omnimarket.handlers.floor_fill_state_store_postgres``). The
freestanding imperative-IO scanner of ``omnibase_core`` reports that call as a blocking finding unless the line
carries the sanctioned inline ``# no-contract-check: <reason>`` boundary mark (the same mark
``omnimarket.projection.postgres_sync_database`` uses). These two tests drive the scanner over the store's own
source, read from the module's file (``inspect.getsourcefile``), never a hard-coded path: the marked source must
show the connection as a declared, suppressed boundary, and the same source with the mark removed must show it as an
active finding.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
from omnibase_core.enums.governance.enum_compliance_violation import (
    EnumComplianceViolation,
)
from omnibase_core.enums.governance.enum_reachability import EnumReachability
from omnibase_core.models.governance.model_freestanding_imperative_result import (
    ModelFreestandingImperativeResult,
)
from omnibase_core.models.nodes.imperative_contract_guard.model_freestanding_scan_input import (
    ModelFreestandingScanInput,
)
from omnibase_core.models.nodes.imperative_contract_guard.model_guard_module_source import (
    ModelGuardModuleSource,
)
from omnibase_core.nodes.node_imperative_contract_guard_check_effect.handler_freestanding_imperative_io import (
    scan_freestanding_imperative_io,
)

from omnimarket.handlers import floor_fill_state_store_postgres

# The scanner's sanctioned per-line boundary mark: a ``# no-contract-check: <reason>`` comment on the call's line.
_BOUNDARY_MARK = re.compile(r"\s*#\s*no-contract-check\s*:.*$", re.MULTILINE)


@pytest.fixture(scope="module")
def store_source() -> str:
    """The store module's own source, read from its file (never a hard-coded path)."""
    module_file = inspect.getsourcefile(floor_fill_state_store_postgres)
    assert module_file is not None, "the store module must be importable from a file"
    return Path(module_file).read_text(encoding="utf-8")


def _repo_relative_path() -> str:
    """The repository-relative POSIX path of the store module (``src/<package>/<module>.py``)."""
    dotted = floor_fill_state_store_postgres.__name__
    return "src/" + dotted.replace(".", "/") + ".py"


def _scan(source: str) -> ModelFreestandingImperativeResult:
    """Run the freestanding imperative-IO scanner over one source string, judged as a live module."""
    return scan_freestanding_imperative_io(
        ModelFreestandingScanInput(
            module=ModelGuardModuleSource(path=_repo_relative_path(), source=source),
            repo="omnimarket",
            allowlisted=False,
            reachability=EnumReachability.LIVE,
        )
    )


def scan_has_no_active_finding(source: str) -> bool:
    """True when the guard's scan of one live module source reports no active (unsuppressed) finding."""
    return all(finding.suppressed for finding in _scan(source).findings)


def test_store_connection_is_a_declared_boundary_for_the_guard(
    store_source: str,
) -> None:
    """The store's connection is a declared boundary: at least one finding, and every one of them suppressed."""
    result = _scan(store_source)
    connections = [
        finding
        for finding in result.findings
        if finding.violation is EnumComplianceViolation.DIRECT_DB
    ]
    assert connections, (
        "the scanner must report the store's psycopg2 connection as a finding"
    )
    assert all(finding.suppressed for finding in result.findings), (
        "every finding of the store's connection must be a declared (suppressed) boundary, not an active one"
    )


def test_guard_boundary_control_unmarked_connection_is_active(
    store_source: str,
) -> None:
    """The mark is what separates the two verdicts: the shipped source has no active finding, the unmarked copy does."""
    shipped_active = [
        finding for finding in _scan(store_source).findings if not finding.suppressed
    ]
    assert not shipped_active, "the shipped store source must carry no active finding"
    unmarked = _BOUNDARY_MARK.sub("", store_source)
    result = _scan(unmarked)
    active = [finding for finding in result.findings if not finding.suppressed]
    assert any(
        finding.violation is EnumComplianceViolation.DIRECT_DB for finding in active
    ), (
        "with the boundary mark removed the connection must be an active (unsuppressed) finding"
    )
