# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The full-suite host guard node answers per host and login (OMN-23367, OMN-23355).

Golden chain: a request carrying the host table and the per-login overlay goes through the handler and
the decision names the row, the login's own uv and run folder, and whether the run is allowed. Error
chain: a malformed overlay raises instead of reading as absent, and a host that is not designated is
refused with the text that names the private table. The injected break test shows the reading fails
for real when the per-login rows stop being applied. The logins and paths here are invented.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_pytest_full_suite_host_guard import (
    HandlerPytestFullSuiteHostGuard,
)
from omnimarket.nodes.node_pytest_full_suite_host_guard.handlers import (
    handler_pytest_full_suite_host_guard as handler_module,
)
from omnimarket.nodes.node_pytest_full_suite_host_guard.models import (
    ModelPytestFullSuiteHostGuardRequest,
)
from tests.scripts._prepush_private_table import HOST_E, SYNTHETIC_TABLE

FIRST = "first-login"
SECOND = "second-login"
SECOND_UV = "/opt/second-login/bin/uv"
SECOND_WORKROOT = "/srv/second-login/onex-prepush"
ROW_UV = "/opt/synthetic/bin/uv"
ROW_WORKROOT = "/srv/onex-prepush"
PRIVATE_REL = "config/onex/overlays/prepush_hosts/prepush_hosts.example.tsv"
HOST_REL = "scripts/hooks/prepush_hosts.tsv"
OVERLAY = (
    "# invented rows\ntable: |\n  # label login uv_abs_path workroot\n"
    f"  h051 {SECOND} {SECOND_UV} {SECOND_WORKROOT}\nother: 1\n"
)


def ask(
    *, host: str = HOST_E, login: str = SECOND, overlay: str | None = OVERLAY
) -> ModelPytestFullSuiteHostGuardRequest:
    return ModelPytestFullSuiteHostGuardRequest(
        table_text=SYNTHETIC_TABLE,
        login_overlay_text=overlay,
        login=login,
        host=host,
        private_table_rel=PRIVATE_REL,
        host_table_rel=HOST_REL,
    )


def test_golden_second_login_on_a_designated_host_runs_with_its_own_uv_and_workroot() -> (
    None
):
    decision = HandlerPytestFullSuiteHostGuard().handle(ask())
    assert decision.allowed is True
    assert decision.verdict == "authorized"
    assert (decision.row, decision.uv, decision.workroot) == (
        "h051",
        SECOND_UV,
        SECOND_WORKROOT,
    )
    assert decision.decision_line == (
        f"prepush-guard decision: host={HOST_E} login={SECOND} row=h051 "
        f"verdict=authorized uv={SECOND_UV} workroot={SECOND_WORKROOT}"
    )
    assert decision.message is None


def test_golden_first_login_keeps_the_host_rows_values() -> None:
    decision = HandlerPytestFullSuiteHostGuard().handle(ask(login=FIRST))
    assert (decision.uv, decision.workroot) == (ROW_UV, ROW_WORKROOT)
    assert HOST_E in decision.designated_hostnames


def test_golden_no_overlay_leaves_the_table_byte_identical() -> None:
    decision = HandlerPytestFullSuiteHostGuard().handle(ask(overlay=None))
    assert decision.merged_table_text == SYNTHETIC_TABLE
    assert (decision.uv, decision.workroot) == (ROW_UV, ROW_WORKROOT)


def test_golden_row_override_replaces_the_hostname_of_its_row() -> None:
    request = ask(host="override-host").model_copy(
        update={"env_overrides": {"PREPUSH_HOST_OVERRIDE_H051": "override-host"}}
    )
    assert HandlerPytestFullSuiteHostGuard().handle(request).allowed is True
    assert HandlerPytestFullSuiteHostGuard().handle(ask(host=HOST_E)).allowed is True
    replaced = ask(host=HOST_E).model_copy(
        update={"env_overrides": {"PREPUSH_HOST_OVERRIDE_H051": "override-host"}}
    )
    assert HandlerPytestFullSuiteHostGuard().handle(replaced).allowed is False


def test_error_host_that_is_not_designated_is_refused_with_the_private_table_named() -> (
    None
):
    decision = HandlerPytestFullSuiteHostGuard().handle(ask(host="not-a-host"))
    assert decision.allowed is False
    assert decision.verdict == "refused"
    assert decision.message is not None
    assert PRIVATE_REL in decision.message
    assert HOST_REL in decision.message
    assert "verdict=refused reason=not-designated" in decision.decision_line


def test_error_unknown_host_identity_is_never_refused() -> None:
    assert HandlerPytestFullSuiteHostGuard().handle(ask(host="")).allowed is True


@pytest.mark.parametrize(
    ("overlay", "match"),
    [
        (
            f"table: |\n  h051 {SECOND} ~/uv {SECOND_WORKROOT}\n",
            "per-login host row",
        ),
        (f"table: |\n  h051 {SECOND} {SECOND_UV}\n", "per-login host row"),
        (
            f"table: |\n  h051 {SECOND} /a /b\n  h051 {SECOND} /c /d\n",
            "repeated",
        ),
        ("other: 1\n", "no `table: |` block"),
    ],
)
def test_error_malformed_overlay_raises_instead_of_reading_as_absent(
    overlay: str, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        HandlerPytestFullSuiteHostGuard().handle(ask(overlay=overlay))


def test_injected_break_that_skips_the_login_rows_fails_the_golden_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        handler_module, "apply_login_rows", lambda table, *_, **__: table
    )
    broken = HandlerPytestFullSuiteHostGuard().handle(ask())
    assert (broken.uv, broken.workroot) == (ROW_UV, ROW_WORKROOT)
    assert broken.uv != SECOND_UV, "the golden reading above would read red here"
