"""Ticket falsifiers pinning the existing operator reply check (OMN-20894)."""

import pytest

from omnimarket.nodes.node_operator_access_mode_effect.handlers.handler_operator_reply_check import (
    HandlerOperatorReplyCheck,
)
from omnimarket.nodes.node_operator_access_mode_effect.models import (
    EnumAccessMode,
    ModelOperatorReplyRequest,
)

pytestmark = pytest.mark.unit
LOCAL_PATH_REPLY = "The report is at /private/tmp/report.md."


def test_reply_with_local_path_refuses_in_remote() -> None:
    verdict = HandlerOperatorReplyCheck().handle(
        ModelOperatorReplyRequest(LOCAL_PATH_REPLY, EnumAccessMode.REMOTE)
    )
    assert not verdict.allowed
    assert "paste" in verdict.reason.lower()
    assert "part that matters" in verdict.reason.lower()


def test_reply_with_bare_ticket_id_refuses_in_remote() -> None:
    verdict = HandlerOperatorReplyCheck().handle(
        ModelOperatorReplyRequest("OMN-20894 is done.", EnumAccessMode.REMOTE)
    )
    assert not verdict.allowed
    assert "name each one in plain words" in verdict.reason.lower()
    assert "OMN-20894" in verdict.reason


def test_same_reply_allows_at_terminal() -> None:
    verdict = HandlerOperatorReplyCheck().handle(
        ModelOperatorReplyRequest(LOCAL_PATH_REPLY, EnumAccessMode.LOCAL)
    )
    assert verdict.allowed


def test_named_id_passes_in_remote() -> None:
    verdict = HandlerOperatorReplyCheck().handle(
        ModelOperatorReplyRequest(
            "The operator reply check (OMN-20894) is merged.", EnumAccessMode.REMOTE
        )
    )
    assert verdict.allowed
