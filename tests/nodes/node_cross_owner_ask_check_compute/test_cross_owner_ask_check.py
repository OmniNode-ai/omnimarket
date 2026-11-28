# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An ask to another owner is refused when the owner's file already holds the object."""

from pathlib import Path

import pytest
import yaml

from omnimarket.models.cross_owner_ask.enum_cross_owner_ask_verdict import (
    EnumCrossOwnerAskVerdict,
)
from omnimarket.models.cross_owner_ask.model_cross_owner_ask_request import (
    ModelCrossOwnerAskRequest,
)
from omnimarket.nodes.node_cross_owner_ask_check_compute.handlers.handler_cross_owner_ask_check import (
    HandlerCrossOwnerAskCheck,
    canonical_read_command,
)

pytestmark = pytest.mark.unit

_REPO = "cloud_repo"
_BASE = "dev"
_PATH = "db/migrations/application-relation-ownership.yaml"
_SEQUENCE = "tenant_event_outbox_id_seq"
# The shape of the real entry: a bare name under a domain, no schema.
_FILE = f"""\
database_objects:
  - name: other_seq
    kind: sequence
    domain: TENANT
  - name: {_SEQUENCE}
    kind: sequence
    domain: OMNINODE_INTERNAL
    dependencies: [tenant_event_outbox]
"""


def _request(
    *,
    names: tuple[str, ...] = (f"public.{_SEQUENCE}",),
    command: str | None = None,
    exit_code: int = 0,
    text: str = _FILE,
) -> ModelCrossOwnerAskRequest:
    return ModelCrossOwnerAskRequest(
        repo=_REPO,
        base=_BASE,
        path=_PATH,
        names=names,
        read_command=command or canonical_read_command(_REPO, _BASE, _PATH),
        read_exit_code=exit_code,
        observed_text=text,
    )


def test_qualified_ask_for_an_entry_declared_bare_is_refused_as_present() -> None:
    result = HandlerCrossOwnerAskCheck().handle(_request())

    assert result.verdict is EnumCrossOwnerAskVerdict.PRESENT
    assert not result.draft_allowed
    assert f"line 5: - name: {_SEQUENCE}" in result.matches[0]
    assert result.verified_by.startswith(
        f'verified by: git -C "${{OMNI_HOME:?}}/{_REPO}" show origin/{_BASE}:{_PATH}'
    )
    assert "already declared" in result.verified_by


def test_object_the_file_does_not_hold_lets_the_draft_go_on() -> None:
    """Positive control: the refusal is not a blanket one."""
    result = HandlerCrossOwnerAskCheck().handle(
        _request(names=("public.never_declared_seq",))
    )

    assert result.verdict is EnumCrossOwnerAskVerdict.ABSENT
    assert result.draft_allowed
    assert "no match in 8 lines" in result.verified_by


def test_a_name_that_only_contains_the_object_is_not_a_match() -> None:
    result = HandlerCrossOwnerAskCheck().handle(_request(names=("outbox_id_seq",)))

    assert result.verdict is EnumCrossOwnerAskVerdict.ABSENT


def test_read_through_another_ref_does_not_count() -> None:
    result = HandlerCrossOwnerAskCheck().handle(
        _request(command=f"git -C /elsewhere/{_REPO} show origin/main:{_PATH}")
    )

    assert result.verdict is EnumCrossOwnerAskVerdict.NONCANONICAL_READ
    assert not result.draft_allowed


@pytest.mark.parametrize(("exit_code", "text"), [(128, _FILE), (0, "  \n")])
def test_unread_file_refuses_the_draft(exit_code: int, text: str) -> None:
    result = HandlerCrossOwnerAskCheck().handle(
        _request(exit_code=exit_code, text=text)
    )

    assert result.verdict is EnumCrossOwnerAskVerdict.UNREADABLE
    assert not result.draft_allowed


_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_cross_owner_ask_check_compute/contract.yaml"
)
_COMPLETED_TOPIC = "onex.evt.omnimarket.cross-owner-ask-check-completed.v1"
_FAILED_TOPIC = "onex.evt.omnimarket.cross-owner-ask-check-failed.v1"


def test_contract_declares_both_terminal_events() -> None:
    """The node publishes a completed and a failed terminal event, each granted write."""
    contract = yaml.safe_load(_CONTRACT.read_text())
    assert contract["runtime_dispatch"]["terminal_events"] == {
        "success": _COMPLETED_TOPIC,
        "failure": _FAILED_TOPIC,
    }
    assert contract["event_bus"]["publish_topics"] == [_COMPLETED_TOPIC, _FAILED_TOPIC]
    written = {
        g["name"]
        for entry in contract["broker_acl_grants"]
        for g in entry["grants"]
        if g["operation"] == "WRITE"
    }
    assert written == {_COMPLETED_TOPIC, _FAILED_TOPIC}
