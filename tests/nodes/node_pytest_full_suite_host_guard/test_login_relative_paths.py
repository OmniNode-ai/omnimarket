# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A per-login row may give its uv and run folder relative to the login's home (OMN-23551).

Golden chain: a relative row resolves under the home the host's account database gives the login, and
absolute rows keep working. Error chain: a relative row for a login with no home, a ``~`` path and a
``..`` path refuse. The seam test pins the request fields the guard script fills. The injected break
shows the golden reading red when the home is not applied. The logins and paths are invented.
"""

from __future__ import annotations

import pwd

import pytest

from omnimarket.nodes.node_pytest_full_suite_host_guard import (
    HandlerPytestFullSuiteHostGuard,
)
from omnimarket.nodes.node_pytest_full_suite_host_guard.adapters import (
    host_table_reader,
)
from omnimarket.nodes.node_pytest_full_suite_host_guard.handlers import (
    handler_pytest_full_suite_host_guard as handler_module,
)
from omnimarket.nodes.node_pytest_full_suite_host_guard.models import (
    ModelPytestFullSuiteHostGuardRequest,
)
from tests.scripts._prepush_private_table import HOST_E, SYNTHETIC_TABLE

SECOND = "second-login"
HOME = "/srv/homes/second-login"
UV_REL = ".local/bin/uv"
WORK_REL = "onex-prepush"
PRIVATE_REL = "config/onex/overlays/prepush_hosts/prepush_hosts.example.tsv"
HOST_REL = "scripts/hooks/prepush_hosts.tsv"


def overlay(uv: str, work: str) -> str:
    return f"table: |\n  h051 {SECOND} {uv} {work}\nother: 1\n"


def ask(
    text: str, *, home: str = HOME, login: str = SECOND
) -> ModelPytestFullSuiteHostGuardRequest:
    return ModelPytestFullSuiteHostGuardRequest(
        table_text=SYNTHETIC_TABLE,
        login_overlay_text=text,
        login=login,
        login_home=home,
        host=HOST_E,
        private_table_rel=PRIVATE_REL,
        host_table_rel=HOST_REL,
    )


def test_golden_relative_row_resolves_under_the_login_home() -> None:
    decision = HandlerPytestFullSuiteHostGuard().handle(ask(overlay(UV_REL, WORK_REL)))
    assert decision.allowed is True
    assert (decision.uv, decision.workroot) == (
        f"{HOME}/{UV_REL}",
        f"{HOME}/{WORK_REL}",
    )
    assert decision.decision_line == (
        f"prepush-guard decision: host={HOST_E} login={SECOND} row=h051 "
        f"verdict=authorized uv={HOME}/{UV_REL} workroot={HOME}/{WORK_REL}"
    )


def test_golden_home_with_trailing_slash_and_absolute_rows_keep_working() -> None:
    slashed = HandlerPytestFullSuiteHostGuard().handle(
        ask(overlay(UV_REL, WORK_REL), home=HOME + "/")
    )
    assert slashed.uv == f"{HOME}/{UV_REL}"
    absolute = HandlerPytestFullSuiteHostGuard().handle(
        ask(overlay("/opt/x/bin/uv", "/srv/x/onex-prepush"), home="")
    )
    assert (absolute.uv, absolute.workroot) == ("/opt/x/bin/uv", "/srv/x/onex-prepush")


def test_golden_another_login_with_no_home_is_not_touched_by_a_relative_row() -> None:
    decision = HandlerPytestFullSuiteHostGuard().handle(
        ask(overlay(UV_REL, WORK_REL), home="", login="first-login")
    )
    assert decision.uv == "/opt/synthetic/bin/uv"


def test_error_relative_row_for_a_login_with_no_home_refuses() -> None:
    with pytest.raises(ValueError, match="has no home directory"):
        HandlerPytestFullSuiteHostGuard().handle(
            ask(overlay(UV_REL, WORK_REL), home="")
        )


@pytest.mark.parametrize(
    ("uv", "work"),
    [
        ("~/.local/bin/uv", WORK_REL),
        (UV_REL, "../onex-prepush"),
        (UV_REL, "a/../../b"),
        ("/opt/x/../uv", WORK_REL),
    ],
)
def test_error_tilde_and_parent_segments_refuse(uv: str, work: str) -> None:
    with pytest.raises(ValueError, match=r"never `~` or `\.\.`"):
        HandlerPytestFullSuiteHostGuard().handle(ask(overlay(uv, work)))


def test_injected_break_that_ignores_the_home_fails_the_golden_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handler_module, "resolve_login_path", lambda path, _home: path)
    broken = HandlerPytestFullSuiteHostGuard().handle(ask(overlay(UV_REL, WORK_REL)))
    assert broken.uv == UV_REL
    assert broken.uv != f"{HOME}/{UV_REL}", (
        "the golden reading above would read red here"
    )


def test_login_home_is_read_from_the_account_database_never_a_constant() -> None:
    me = pwd.getpwuid(__import__("os").getuid())
    assert host_table_reader.login_home(me.pw_name) == me.pw_dir
    assert host_table_reader.login_home("") == ""
    assert host_table_reader.login_home("no-such-login-23551") == ""


def test_seam_request_fields_the_guard_script_fills() -> None:
    """Fails when the script's request and the node's model stop agreeing on a field."""
    fields = set(ModelPytestFullSuiteHostGuardRequest.model_fields)
    assert fields == {
        "table_text",
        "login_overlay_text",
        "login",
        "login_home",
        "legacy_override_by_label",
        "host",
        "env_overrides",
        "private_table_rel",
        "host_table_rel",
    }


def test_local_login_is_the_invoking_account() -> None:
    assert host_table_reader.local_login() == __import__("getpass").getuser()
