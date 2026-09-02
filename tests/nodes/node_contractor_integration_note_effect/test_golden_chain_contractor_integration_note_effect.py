# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_contractor_integration_note_effect (OMN-17277).

One frozen end-to-end pass over the real chain — overlay load, PR-JSON parse,
handler dispatch, note render — pinned to the exact text delivered to the
recipient. The two effect boundaries are faked; nothing else is.

Why the whole note is asserted byte-for-byte rather than by substring: this
artifact is read by someone outside the team, and the failure it exists to
prevent is a wording or field change that silently degrades what they receive.
A substring assertion cannot see a dropped field, a leaked internal path
appended by a later edit, or a pin recipe that stopped being runnable. If this
test fails, read the diff as "here is what the contractor would now be sent"
and decide whether that is the intent.

The fixture is the real 2026-09-01 case: omnibase_infra#3120, the overlay fix
the contractor was blocked on, which merged at 15:21Z and sat unannounced for
five hours because the announcement was a manual promise on the ticket.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_contractor_integration_note_effect.cli import (
    load_roster,
    parse_pull_request,
    run,
)
from omnimarket.nodes.node_contractor_integration_note_effect.handlers.handler_contractor_integration_note import (
    HandlerContractorIntegrationNote,
)
from omnimarket.nodes.node_contractor_integration_note_effect.models.model_integration_note_request import (
    ModelIntegrationNoteRequest,
    ModelTicketFacts,
)
from omnimarket.nodes.node_contractor_integration_note_effect.models.model_integration_note_result import (
    ModelIntegrationNoteResult,
)
from omnimarket.nodes.node_contractor_integration_note_effect.services.note_composer import (
    parse_note_keys,
)

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ROSTER_PATH = _REPO_ROOT / "config" / "contractor_roster.yaml"

# The Linear user UUID of the contractor, read from the SHIPPED overlay rather
# than restated here — a golden chain that hardcodes the roster would keep
# passing after the roster changed.
_CONTRACTOR_INDEX = 0

_PR_PAYLOAD: dict[str, object] = {
    "number": 3120,
    "title": (
        "fix(OMN-17150): resolve the Bifrost lane overlay from the lane's own "
        "pin, never a dev-lane default"
    ),
    "body": (
        "## OMN-17150 — every lane fell through to the dev lane's Bifrost overlay\n"
        "\n"
        "The renderer carried a hardcoded default overlay path naming the dev "
        "lane's file, so any lane that did not pass one explicitly resolved the "
        "dev lane's overlay.\n"
        "\n"
        "## Integration note\n"
        "What it means for your surfaces: C4 — your lane boots instead of "
        "crash-looping on a missing overlay.\n"
        "Probe to run: curl -sf http://localhost:58085/health\n"
        "Pass expectation: HTTP 200 and a JSON body whose status is ok.\n"
    ),
    "merged": True,
    "merge_commit_sha": "3a29fd26d0000000000000000000000000000000",
    "merged_at": "2026-09-01T15:21:04Z",
    "base": {"ref": "dev"},
    "html_url": "https://github.com/OmniNode-ai/omnibase_infra/pull/3120",
}

_EXPECTED_NOTE = """\
**INTEGRATION NOTE — OMN-17150**

_For {name}. Posted automatically when this merge landed; no action was needed \
from you to receive it._

**What changed**
The renderer carried a hardcoded default overlay path naming the dev lane's \
file, so any lane that did not pass one explicitly resolved the dev lane's \
overlay.

**What it means for your surfaces**
C4 — your lane boots instead of crash-looping on a missing overlay.

**Probe to run**
curl -sf http://localhost:58085/health

**Pass expectation**
HTTP 200 and a JSON body whose status is ok.

**Reachable when**
Not in a released tag yet — this is on `dev` only. To reach it before the next \
release, pin the merge commit:

    uv pip install "git+https://github.com/OmniNode-ai/omnibase_infra@\
3a29fd26d0000000000000000000000000000000"

**Delivery facts**

- Repo: `OmniNode-ai/omnibase_infra`
- PR: [#3120](https://github.com/OmniNode-ai/omnibase_infra/pull/3120) — \
fix(OMN-17150): resolve the Bifrost lane overlay from the lane's own pin, \
never a dev-lane default
- Merged: 2026-09-01T15:21:04Z into `dev`
- Merge commit: `3a29fd26d0000000000000000000000000000000`

integration-note-key: OmniNode-ai/omnibase_infra#3120"""


class _FakeLinear:
    """Records what the chain reads and writes at the Linear boundary."""

    def __init__(self, ticket: ModelTicketFacts) -> None:
        self._ticket = ticket
        self.comments: list[str] = []

    def fetch_ticket(self, identifier: str) -> ModelTicketFacts | None:
        return self._ticket if identifier == self._ticket.identifier else None

    def existing_note_keys(self, issue_id: str) -> tuple[str, ...]:
        return parse_note_keys(self.comments)

    def post_note(self, issue_id: str, body: str) -> None:
        self.comments.append(body)


class _FakeReleases:
    def __init__(self, tags: tuple[str, ...] = ()) -> None:
        self._tags = tags

    def tags_containing(self, merge_sha: str) -> tuple[str, ...]:
        return self._tags


def _chain(tmp_path: Path) -> tuple[ModelIntegrationNoteRequest, _FakeLinear]:
    pr_json = tmp_path / "pr.json"
    pr_json.write_text(json.dumps(_PR_PAYLOAD), encoding="utf-8")

    roster = load_roster(_ROSTER_PATH)
    pull_request = parse_pull_request(
        "OmniNode-ai/omnibase_infra",
        json.loads(pr_json.read_text(encoding="utf-8")),
    )
    contractor = roster.contractors[_CONTRACTOR_INDEX]
    linear = _FakeLinear(
        ModelTicketFacts(
            issue_id="11111111-2222-3333-4444-555555555555",
            identifier="OMN-17150",
            title="Bifrost lane overlay resolution",
            assignee_linear_user_id=contractor.linear_user_id,
        )
    )
    return (
        ModelIntegrationNoteRequest(
            pull_request=pull_request,
            roster=roster,
            checkout_path=tmp_path,
        ),
        linear,
    )


def test_golden_chain_delivers_the_frozen_note(tmp_path: Path) -> None:
    request, linear = _chain(tmp_path)
    contractor = request.roster.contractors[_CONTRACTOR_INDEX]

    result = run(request, linear, _FakeReleases())

    assert result.posted is True
    assert len(linear.comments) == 1
    assert linear.comments[0] == _EXPECTED_NOTE.format(name=contractor.display_name)
    assert result.decision.redacted_fields == ()


def test_golden_chain_is_idempotent_across_a_replay(tmp_path: Path) -> None:
    """The second firing of the same merge writes nothing.

    A re-run, a backfill dispatch and a bus replay all land here, so this is the
    property that keeps the mechanism from becoming its own noise source.
    """
    request, linear = _chain(tmp_path)

    HandlerContractorIntegrationNote(linear, _FakeReleases()).handle(request)
    HandlerContractorIntegrationNote(linear, _FakeReleases()).handle(request)
    HandlerContractorIntegrationNote(linear, _FakeReleases()).handle(request)

    assert len(linear.comments) == 1


def test_golden_chain_parse_refuses_an_unmerged_pr() -> None:
    """A closed-unmerged PR carries a null merge SHA; the chain must not run."""
    payload = {**_PR_PAYLOAD, "merged": False}
    with pytest.raises(ValueError, match="not merged"):
        parse_pull_request("OmniNode-ai/omnibase_infra", payload)


def test_golden_chain_output_surface_matches_the_contract() -> None:
    """Pin the node's declared output keys against what the chain returns.

    Pins both halves of the node's declared output surface: the terminal event
    name and the `outputs` keys. A field renamed on one side and not the other
    would otherwise pass every other test in this file.
    """
    import yaml

    contract = yaml.safe_load(
        (
            _REPO_ROOT
            / "src"
            / "omnimarket"
            / "nodes"
            / "node_contractor_integration_note_effect"
            / "contract.yaml"
        ).read_text(encoding="utf-8")
    )
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.contractor-integration-note-posted.v1"
    )
    assert (
        contract["runtime_dispatch"]["terminal_events"]["success"]
        == (contract["terminal_event"])
    )
    assert set(contract["outputs"]) == set(ModelIntegrationNoteResult.model_fields), (
        "contract outputs and the result model have drifted apart"
    )
