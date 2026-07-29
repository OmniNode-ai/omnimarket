# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Focused tests for HandlerDodEvidenceGithubEffect (OMN-14400, RSD-1 of OMN-14398).

Covers the canonical EFFECT handler's ``.handle()`` boundary directly, and
proves ``EvidenceCollector``'s delegating wrapper methods
(``_lookup_pr_for_ticket``, ``_lookup_repo_for_ticket``,
``_fetch_pr_merge_state``, ``_fetch_pr_checks_green``) resolve to identical
results after the carve-out — the whole point of a behavior-identical
refactor. ``subprocess.run`` is patched at the handler module (where the gh
CLI invocations now live); since ``subprocess`` is a singleton module object,
this patches the same attribute the pre-existing
``test_omn_14207_live_pr_state_check.py::TestGhOutputParsing`` suite patches
via ``ec_mod.subprocess`` — both target sets exercise the same underlying
call.
"""

from __future__ import annotations

import subprocess

import pytest
from omnibase_core.enums.enum_node_kind import EnumNodeKind

from omnimarket.nodes.node_dod_verify.handlers import (
    handler_dod_evidence_github_effect as hd_mod,
)
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_evidence_github_effect import (
    HandlerDodEvidenceGithubEffect,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_evidence_github_lookup import (
    EnumDodEvidenceGithubOperation,
    ModelDodEvidenceGithubLookupCommand,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)

_REPO = "OmniNode-ai/omnibase_infra"
_PR = 2216


def _fake_completed(stdout: str, returncode: int = 0, stderr: str = "") -> object:
    def _run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        argv = list(args[0]) if args else []
        return subprocess.CompletedProcess(
            args=argv, returncode=returncode, stdout=stdout, stderr=stderr
        )

    return _run


# ---------------------------------------------------------------------------
# HandlerDodEvidenceGithubEffect.handle() — the canonical EFFECT boundary.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHandlerDodEvidenceGithubEffectHandle:
    def test_handle_returns_effect_output_with_single_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _fake_completed('{"mergedAt":"2026-07-01T00:00:00Z","state":"MERGED"}'),
        )
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.FETCH_PR_MERGE_STATE,
            repo=_REPO,
            pr_number=_PR,
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)

        assert output.node_kind == EnumNodeKind.EFFECT
        assert output.correlation_id == command.correlation_id
        assert len(output.events) == 1
        result = output.events[0]
        assert result.correlation_id == command.correlation_id
        assert result.operation == EnumDodEvidenceGithubOperation.FETCH_PR_MERGE_STATE
        assert result.merged is True
        assert result.state == "MERGED"

    def test_unknown_operation_raises(self) -> None:
        # Constructing a command requires a valid enum member, so simulate an
        # unrecognized operation by bypassing validation via model_construct.
        command = ModelDodEvidenceGithubLookupCommand.model_construct(
            operation="not-a-real-operation",  # type: ignore[arg-type]
            correlation_id=ModelDodEvidenceGithubLookupCommand.model_fields[
                "correlation_id"
            ].default_factory(),
        )
        with pytest.raises(ValueError, match="Unknown operation"):
            HandlerDodEvidenceGithubEffect().handle(command)


# ---------------------------------------------------------------------------
# LOOKUP_PR_FOR_TICKET / LOOKUP_REPO_FOR_TICKET
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLookupOperations:
    def test_lookup_pr_for_ticket_resolves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # OMN-15382: LOOKUP_PR_FOR_TICKET now requires an explicit ``repo``
        # (never guesses one) and requests number+title+headRefName so it can
        # filter to an exact ticket-id-token match instead of trusting
        # ``gh``'s fuzzy full-text ranking + a blind ``.[0]`` take.
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _fake_completed(
                '[{"number":2216,"title":"fix(OMN-13996): x",'
                '"headRefName":"jonah/omn-13996-x"}]'
            ),
        )
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.LOOKUP_PR_FOR_TICKET,
            ticket_id="OMN-13996",
            repo=_REPO,
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)
        assert output.events[0].text_value == "2216"
        assert output.events[0].error_code is None

    def test_lookup_pr_for_ticket_missing_repo_fails_closed(self) -> None:
        """OMN-15382: no repo => fail closed WITHOUT ever calling gh."""
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.LOOKUP_PR_FOR_TICKET,
            ticket_id="OMN-13996",
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)
        assert output.events[0].text_value == ""
        assert output.events[0].error_code == "PR_LOOKUP_FAILED"

    def test_lookup_pr_for_ticket_ambiguous_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OMN-15382: 2+ exact-token candidates => fail closed, never the
        most recent."""
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _fake_completed(
                '[{"number":2216,"title":"fix(OMN-13996): x",'
                '"headRefName":"a"},'
                '{"number":2217,"title":"fix(OMN-13996): y",'
                '"headRefName":"b"}]'
            ),
        )
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.LOOKUP_PR_FOR_TICKET,
            ticket_id="OMN-13996",
            repo=_REPO,
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)
        assert output.events[0].text_value == ""
        assert output.events[0].error_code == "PR_LOOKUP_AMBIGUOUS"

    def test_lookup_pr_for_ticket_mismatched_candidate_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OMN-15382: a fuzzy-matched but wrong-ticket candidate (the
        OMN-15382 incident shape — a similarly-worded PR for a different
        ticket ranked first) must not be silently trusted."""
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _fake_completed(
                '[{"number":2454,"title":"fix(OMN-13995): unrelated",'
                '"headRefName":"someone/omn-13995-fix"}]'
            ),
        )
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.LOOKUP_PR_FOR_TICKET,
            ticket_id="OMN-13996",
            repo=_REPO,
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)
        assert output.events[0].text_value == ""
        assert output.events[0].error_code == "PR_LOOKUP_FAILED"

    def test_lookup_pr_for_ticket_gh_invocation_passes_repo_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, list[str]] = {}

        def _run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
            argv = [str(a) for a in (list(args[0]) if args else [])]
            captured["argv"] = argv
            return subprocess.CompletedProcess(
                args=argv,
                returncode=0,
                stdout=(
                    '[{"number":2216,"title":"fix(OMN-13996): x","headRefName":"a"}]'
                ),
                stderr="",
            )

        monkeypatch.setattr(hd_mod.subprocess, "run", _run)
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.LOOKUP_PR_FOR_TICKET,
            ticket_id="OMN-13996",
            repo=_REPO,
        )
        HandlerDodEvidenceGithubEffect().handle(command)
        assert "--repo" in captured["argv"]
        assert _REPO in captured["argv"]

    def test_lookup_pr_for_ticket_no_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(hd_mod.subprocess, "run", _fake_completed("[]"))
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.LOOKUP_PR_FOR_TICKET,
            ticket_id="OMN-99999",
            repo=_REPO,
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)
        assert output.events[0].text_value == ""
        assert output.events[0].error_code == "PR_LOOKUP_FAILED"

    def test_lookup_pr_for_ticket_gh_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(hd_mod.subprocess, "run", _fake_completed("", returncode=1))
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.LOOKUP_PR_FOR_TICKET,
            ticket_id="OMN-13996",
            repo=_REPO,
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)
        assert output.events[0].text_value == ""
        assert output.events[0].error_code == "PR_LOOKUP_FAILED"

    def test_lookup_repo_for_ticket_resolves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _fake_completed(
                '[{"number":2216,"title":"fix(OMN-13996): x",'
                '"headRefName":"a","headRepository":'
                '{"nameWithOwner":"OmniNode-ai/omnibase_infra"}}]'
            ),
        )
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.LOOKUP_REPO_FOR_TICKET,
            ticket_id="OMN-13996",
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)
        assert output.events[0].text_value == "OmniNode-ai/omnibase_infra"
        assert output.events[0].error_code is None

    def test_lookup_repo_for_ticket_no_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(hd_mod.subprocess, "run", _fake_completed("[]"))
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.LOOKUP_REPO_FOR_TICKET,
            ticket_id="OMN-99999",
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)
        assert output.events[0].text_value == ""
        assert output.events[0].error_code == "REPO_LOOKUP_FAILED"

    def test_lookup_repo_for_ticket_ambiguous_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _fake_completed(
                '[{"number":1,"title":"fix(OMN-13996): a","headRefName":"x",'
                '"headRepository":{"nameWithOwner":"OmniNode-ai/omnimarket"}},'
                '{"number":2,"title":"fix(OMN-13996): b","headRefName":"y",'
                '"headRepository":{"nameWithOwner":"OmniNode-ai/omnibase_core"}}]'
            ),
        )
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.LOOKUP_REPO_FOR_TICKET,
            ticket_id="OMN-13996",
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)
        assert output.events[0].text_value == ""
        assert output.events[0].error_code == "REPO_LOOKUP_AMBIGUOUS"


# ---------------------------------------------------------------------------
# FETCH_PR_CHECKS_GREEN — regression guard for the OMN-14390 ``--required``
# flag (this handler must carry it forward exactly, or a non-required
# advisory check would wrongly block a Done-flip again).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFetchPrChecksGreenRequiredFlag:
    def test_gh_invocation_passes_required_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, list[str]] = {}

        def _run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
            argv = list(args[0]) if args else []
            captured["argv"] = [str(a) for a in argv]
            return subprocess.CompletedProcess(
                args=argv,
                returncode=0,
                stdout='[{"name":"Lint","state":"SUCCESS"}]',
                stderr="",
            )

        monkeypatch.setattr(hd_mod.subprocess, "run", _run)
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.FETCH_PR_CHECKS_GREEN,
            repo=_REPO,
            pr_number=_PR,
        )
        HandlerDodEvidenceGithubEffect().handle(command)
        assert "--required" in captured["argv"]


# ---------------------------------------------------------------------------
# EvidenceCollector delegation — proves behavior-identical parity: the
# public wrapper methods on EvidenceCollector keep their exact pre-refactor
# signatures/return shapes after delegating to the new EFFECT handler.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEvidenceCollectorDelegatesToEffectHandler:
    def test_lookup_pr_for_ticket_delegates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # OMN-15382: PR lookup now REQUIRES a resolvable repo before it will
        # call gh at all; REPO env var is the simplest way to supply one from
        # outside an evidence-item context.
        monkeypatch.delenv("PR_NUMBER", raising=False)
        monkeypatch.setenv("REPO", _REPO)
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _fake_completed(
                '[{"number":2216,"title":"fix(OMN-13996): x","headRefName":"a"}]'
            ),
        )
        collector = EvidenceCollector()
        assert collector._lookup_pr_for_ticket("OMN-13996") == "2216"

    def test_lookup_pr_for_ticket_no_repo_fails_closed_without_gh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OMN-15382: no REPO env var, no id-derived binding => fail closed
        WITHOUT ever calling gh (closes the root cause: the prior code ran an
        unscoped ``gh pr list`` that resolved whatever repo the process cwd's
        git remote pointed at)."""
        monkeypatch.delenv("PR_NUMBER", raising=False)
        monkeypatch.delenv("REPO", raising=False)

        def _boom(*args: object, **kwargs: object) -> None:
            raise AssertionError("gh must not be invoked with no resolvable repo")

        monkeypatch.setattr(hd_mod.subprocess, "run", _boom)
        collector = EvidenceCollector()
        assert collector._lookup_pr_for_ticket("OMN-13996") == ""
        assert collector._last_pr_lookup_error is not None
        assert "PR_LOOKUP_FAILED" in collector._last_pr_lookup_error

    def test_lookup_pr_for_ticket_id_derived_binding_skips_gh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OMN-15382: when the current evidence item's id follows the
        ``dod-<owner>-<repo>-pr-<number>`` autobind convention, both repo and
        PR number resolve from it directly — zero gh calls."""
        monkeypatch.delenv("PR_NUMBER", raising=False)
        monkeypatch.delenv("REPO", raising=False)

        def _boom(*args: object, **kwargs: object) -> None:
            raise AssertionError("gh must not be invoked for an id-derived binding")

        monkeypatch.setattr(hd_mod.subprocess, "run", _boom)
        collector = EvidenceCollector()
        collector._current_evidence_item_id = "dod-OmniNode-ai-omnibase_infra-pr-2536"
        assert collector._lookup_pr_for_ticket("OMN-13996") == "2536"

    def test_lookup_pr_for_ticket_env_short_circuit_skips_gh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PR_NUMBER", "4242")

        def _boom(*args: object, **kwargs: object) -> None:
            raise AssertionError("gh must not be invoked when PR_NUMBER is set")

        monkeypatch.setattr(hd_mod.subprocess, "run", _boom)
        collector = EvidenceCollector()
        assert collector._lookup_pr_for_ticket("OMN-13996") == "4242"

    def test_lookup_repo_for_ticket_delegates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("REPO", raising=False)
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _fake_completed(
                '[{"number":2216,"title":"fix(OMN-13996): x",'
                '"headRefName":"a","headRepository":'
                '{"nameWithOwner":"OmniNode-ai/omnibase_infra"}}]'
            ),
        )
        collector = EvidenceCollector()
        assert (
            collector._lookup_repo_for_ticket("OMN-13996")
            == "OmniNode-ai/omnibase_infra"
        )

    def test_fetch_pr_merge_state_delegates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _fake_completed('{"mergedAt":null,"state":"OPEN"}'),
        )
        collector = EvidenceCollector()
        assert collector._fetch_pr_merge_state(_REPO, _PR) == (False, "OPEN")

    def test_fetch_pr_merge_state_unresolvable_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(hd_mod.subprocess, "run", _fake_completed("", returncode=1))
        collector = EvidenceCollector()
        assert collector._fetch_pr_merge_state(_REPO, _PR) is None

    def test_fetch_pr_checks_green_delegates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = (
            '[{"name":"Lint","state":"FAILURE"},{"name":"Build","state":"SUCCESS"}]'
        )
        monkeypatch.setattr(hd_mod.subprocess, "run", _fake_completed(payload))
        collector = EvidenceCollector()
        green, detail = collector._fetch_pr_checks_green(_REPO, _PR)
        assert green is False
        assert "Lint" in detail
