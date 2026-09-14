# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18332 AC6. The LIVE mint path transcribes, not just one of its two halves.

The transcription landed on the ``node_occ_state_effect`` ->
``node_occ_companion_compute`` producer and was proven there. It was NOT wired
into the BORN-path producer (``node_pr_lifecycle_fix_effect`` ->
:class:`OccCompanionEmitter`), which is the one that mints most companions. The
measurement that forced this module: of the nine companions minted between the
effects container's 2026-09-14T03:38:44Z restart and 06:1xZ, seven carry
``runner: node_pr_lifecycle_fix_effect`` and none of the nine carries a
``binds_ac`` key, while the deployed emitter contains zero occurrences of the
string ``ac_bindings`` and the deployed renderer that would emit it sits unused
in the emitter's OWN package.

The tests here are the born-path twins of the compute-path tests in
``test_omn_18332_creation_revision_transcription.py``. They assert three things
and nothing else:

* the born path's contract renderers ACCEPT bindings and emit them on the
  downstream item, exactly where the compute twin emits them;
* a ticket that declared no falsifiers renders bytes IDENTICAL to today's, so
  wiring the read cannot change a contract it has nothing to say about;
* both producers resolve their bindings through ONE reader, so a fix to the
  Linear read can never again reach one producer and miss the other.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    render_companion_contract,
    render_downstream_dod_evidence_item,
)
from omnimarket.occ_ac_transcription import AUTOBINDER_IDENTITY, ModelTranscribedBinding

pytestmark = pytest.mark.unit


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _accepted(label: str) -> ModelTranscribedBinding:
    return ModelTranscribedBinding(
        label=label,
        criterion_hash=_hash(f"criterion {label}"),
        falsifier=f"pytest -k {label}",
        proposed_by=AUTOBINDER_IDENTITY,
        accepted_by="user-id-of-the-author",
        accepted_at=datetime(2026, 9, 13, 21, 57, 10, tzinfo=UTC),
    )


def _draft(label: str) -> ModelTranscribedBinding:
    return ModelTranscribedBinding(
        label=label,
        criterion_hash=_hash(f"criterion {label} as edited"),
        falsifier=f"pytest -k {label}",
        proposed_by=AUTOBINDER_IDENTITY,
    )


class TestBornPathContractCarriesBindings:
    """AC6 falsifier: a born-path contract rendered with bindings names them."""

    def test_fresh_contract_emits_binds_ac_and_records(self) -> None:
        rendered = render_companion_contract(
            ticket_id="OMN-18332",
            repo="OmniNode-ai/omnimarket",
            pr_number=2528,
            evidence_id="dod-OmniNode-ai-omnimarket-pr-2528",
            ac_bindings=(_accepted("AC1"), _draft("AC2")),
        )
        assert "    binds_ac:\n" in rendered
        assert '      - "AC1"\n' in rendered
        assert '      - "AC2"\n' in rendered
        assert "    ac_bindings:\n" in rendered
        # The accepted record carries an acceptor; the draft carries none. A
        # renderer that emitted acceptance for both would pass a bare presence
        # assertion, so count the acceptances.
        assert rendered.count('accepted_by: "user-id-of-the-author"') == 1
        assert rendered.count(f'proposed_by: "{AUTOBINDER_IDENTITY}"') == 2

    def test_repair_row_for_a_preexisting_contract_emits_them_too(self) -> None:
        """The F-04 repair path appends the same row, so it carries the same block.

        A contract another PR already authored is repaired by appending this
        PR's base rows. If only the fresh-contract renderer learned bindings,
        every second and later companion on a shared ticket would silently mint
        unbound -- the same half-wired failure this ticket is fixing.
        """
        rendered = render_downstream_dod_evidence_item(
            evidence_id="dod-OmniNode-ai-omnimarket-pr-2528",
            repo="OmniNode-ai/omnimarket",
            pr_number=2528,
            ac_bindings=(_accepted("AC3"),),
        )
        assert "    binds_ac:\n" in rendered
        assert '      - "AC3"\n' in rendered
        assert "    ac_bindings:\n" in rendered


class TestUndeclaredTicketRendersTodaysBytes:
    """A ticket that declared no falsifiers is byte-identical to today."""

    def test_fresh_contract_unchanged(self) -> None:
        with_empty = render_companion_contract(
            ticket_id="OMN-18332",
            repo="OmniNode-ai/omnimarket",
            pr_number=2528,
            evidence_id="dod-OmniNode-ai-omnimarket-pr-2528",
            ac_bindings=(),
        )
        without = render_companion_contract(
            ticket_id="OMN-18332",
            repo="OmniNode-ai/omnimarket",
            pr_number=2528,
            evidence_id="dod-OmniNode-ai-omnimarket-pr-2528",
        )
        assert with_empty == without
        assert "binds_ac" not in with_empty

    def test_repair_row_unchanged(self) -> None:
        with_empty = render_downstream_dod_evidence_item(
            evidence_id="dod-OmniNode-ai-omnimarket-pr-2528",
            repo="OmniNode-ai/omnimarket",
            pr_number=2528,
            ac_bindings=(),
        )
        without = render_downstream_dod_evidence_item(
            evidence_id="dod-OmniNode-ai-omnimarket-pr-2528",
            repo="OmniNode-ai/omnimarket",
            pr_number=2528,
        )
        assert with_empty == without
        assert "binds_ac" not in with_empty


class TestOneReaderServesBothProducers:
    """Both producers call the SAME Linear read; there is no second copy."""

    def test_emitter_reads_bindings_through_the_shared_reader(self) -> None:
        from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers import (
            occ_companion_emitter,
        )
        from omnimarket.occ_ticket_bindings import read_ticket_ac_bindings

        assert occ_companion_emitter.read_ticket_ac_bindings is read_ticket_ac_bindings

    def test_state_effect_reads_bindings_through_the_shared_reader(self) -> None:
        from omnimarket.nodes.node_occ_state_effect.handlers import (
            handler_occ_state_effect,
        )
        from omnimarket.occ_ticket_bindings import read_ticket_ac_bindings

        assert (
            handler_occ_state_effect.read_ticket_ac_bindings is read_ticket_ac_bindings
        )


class TestSharedReaderFailsClosed:
    """Every Linear failure degrades to no bindings, never to an acceptance."""

    def test_no_key_in_the_environment_yields_no_bindings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omnimarket.occ_creation_revision import LINEAR_API_KEY_ENV
        from omnimarket.occ_ticket_bindings import read_ticket_ac_bindings

        monkeypatch.delenv(LINEAR_API_KEY_ENV, raising=False)
        assert read_ticket_ac_bindings("OMN-18332") == ()

    def test_an_unresolvable_issue_yields_no_bindings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omnimarket import occ_ticket_bindings

        def _no_issue(query: str, variables: dict[str, object]) -> dict[str, object]:
            return {"issue": None}

        monkeypatch.setattr(occ_ticket_bindings, "linear_graphql", _no_issue)
        assert occ_ticket_bindings.read_ticket_ac_bindings("OMN-18332") == ()

    def test_a_transport_failure_yields_no_bindings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omnimarket import occ_ticket_bindings

        def _boom(query: str, variables: dict[str, object]) -> dict[str, object]:
            raise OSError("broker down")

        monkeypatch.setattr(occ_ticket_bindings, "linear_graphql", _boom)
        assert occ_ticket_bindings.read_ticket_ac_bindings("OMN-18332") == ()
