# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14890: genuine author-identity independence signal.

OMN-13991's shadow-measurement (docs/evidence/OMN-13991/shadow-measurement-
2026-07-20.md) found the strict ``ModelDodReceipt`` gate's self-attestation
rule (``verifier == runner`` literal string equality) is a STRUCTURAL NO-OP
against the real receipt corpus: every producer mints a syntactically
distinct ``verifier`` label by naming convention even when the SAME
agent/human authored both the product fix and its own OCC evidence.
OMN-14431 is the concrete real case: product commit and OCC-evidence commit
in ``onex_change_control`` are both authored by the identical git identity
(``jonah@omninode.ai``) under cosmetically-distinct ``runner``/``verifier``
strings.

This gate replaces label comparison with actual git-identity comparison.
These tests prove:

* the pure helpers (repo extraction, tautological-probe-shape detection,
  identity normalization, the verdict function) behave correctly in
  isolation;
* RED: the OMN-14431-shaped self-bind receipt — same git-author on both
  sides — passes today's loose/strict gates unchanged (flag OFF);
* GREEN: the same receipt is REJECTED once the flag is ON, because
  ``product_author`` and ``occ_author`` resolve to the identical git email;
* a genuinely independent human author PASSES (flag ON);
* a sanctioned-automation ``occ_author`` PASSES even though nothing requires
  the product author to differ from it;
* the narrow self-bind carve-out fires ONLY when the receipt's check_value is
  a tautological existence probe AND the PR's required CI is independently
  green — never on identity alone.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_linear_triage.handlers.handler_linear_triage import (
    AuthorIdentityProbe,
    AuthorIdentitySubprocessProbe,
    OccReceiptSubprocessProbe,
    _author_identity_gate_enabled,
    _author_identity_verdict,
    _build_commit_repo_hints,
    _extract_repo_from_check_value,
    _is_tautological_existence_probe,
    _normalize_identity,
    _occ_receipt_dir,
    _receipt_passes_author_identity_gate,
)

_ENV = "OMNI_LINEAR_TRIAGE_AUTHOR_IDENTITY_GATE"

# The real OMN-14431 self-bind check_value shape (verified live against
# onex_change_control@origin/dev, drift/dod_receipts/OMN-14431/).
_SELF_BIND_CHECK_VALUE = (
    "gh pr view 4523 --repo OmniNode-ai/onex_change_control "
    "--json number,state,headRefName,headRefOid,files"
)
_REAL_CHECK_CHECK_VALUE = (
    "gh pr view 1838 --repo OmniNode-ai/omnimarket --json files,state,mergedAt"
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestExtractRepo:
    def test_repo_with_org_prefix_stripped(self) -> None:
        assert (
            _extract_repo_from_check_value(
                "gh pr view 4523 --repo OmniNode-ai/onex_change_control --json number"
            )
            == "onex_change_control"
        )

    def test_repo_without_org_prefix(self) -> None:
        assert (
            _extract_repo_from_check_value("gh pr checks 99 --repo omnimarket")
            == "omnimarket"
        )

    def test_no_repo_flag_returns_none(self) -> None:
        assert _extract_repo_from_check_value("pytest tests/ -v") is None


class TestTautologicalExistenceProbe:
    def test_self_bind_shape_matches(self) -> None:
        assert _is_tautological_existence_probe(_SELF_BIND_CHECK_VALUE)

    def test_subset_of_fields_still_matches(self) -> None:
        assert _is_tautological_existence_probe(
            "gh pr view 1 --repo OmniNode-ai/x --json number,state"
        )

    def test_real_correctness_field_does_not_match(self) -> None:
        # 'mergedAt' / 'files' diff content is not a pure existence field set
        # once combined with fields outside the closed vocabulary.
        assert not _is_tautological_existence_probe(_REAL_CHECK_CHECK_VALUE)

    def test_non_gh_pr_view_command_does_not_match(self) -> None:
        assert not _is_tautological_existence_probe("pytest tests/ -v")

    def test_empty_check_value_does_not_match(self) -> None:
        assert not _is_tautological_existence_probe("")


class TestNormalizeIdentity:
    def test_strips_and_lowercases(self) -> None:
        assert _normalize_identity("  Jonah@Omninode.AI ", {}) == "jonah@omninode.ai"

    def test_alias_map_resolves_to_canonical(self) -> None:
        aliases = {"alt@omninode.ai": "jonah@omninode.ai"}
        assert _normalize_identity("Alt@Omninode.ai", aliases) == "jonah@omninode.ai"

    def test_unmapped_identity_passes_through_normalized(self) -> None:
        assert _normalize_identity("worker-a", {}) == "worker-a"


class TestAuthorIdentityVerdict:
    def test_same_identity_both_sides_fails(self) -> None:
        assert not _author_identity_verdict(
            check_value="pytest tests/ -v",
            product_author_raw="jonah@omninode.ai",
            occ_author_raw="jonah@omninode.ai",
            aliases={},
            sanctioned=frozenset(),
            ci_checks_green=None,
        )

    def test_distinct_human_identities_passes(self) -> None:
        assert _author_identity_verdict(
            check_value="pytest tests/ -v",
            product_author_raw="alice@omninode.ai",
            occ_author_raw="bob@omninode.ai",
            aliases={},
            sanctioned=frozenset(),
            ci_checks_green=None,
        )

    def test_aliased_identity_collapses_to_same_and_fails(self) -> None:
        assert not _author_identity_verdict(
            check_value="pytest tests/ -v",
            product_author_raw="jonah@omninode.ai",
            occ_author_raw="alt-jonah@omninode.ai",
            aliases={"alt-jonah@omninode.ai": "jonah@omninode.ai"},
            sanctioned=frozenset(),
            ci_checks_green=None,
        )

    def test_sanctioned_occ_author_passes_even_if_equal(self) -> None:
        assert _author_identity_verdict(
            check_value="pytest tests/ -v",
            product_author_raw="bot@omninode.ai",
            occ_author_raw="bot@omninode.ai",
            aliases={},
            sanctioned=frozenset({"bot@omninode.ai"}),
            ci_checks_green=None,
        )

    def test_unresolved_product_author_fails_closed(self) -> None:
        assert not _author_identity_verdict(
            check_value="pytest tests/ -v",
            product_author_raw=None,
            occ_author_raw="jonah@omninode.ai",
            aliases={},
            sanctioned=frozenset(),
            ci_checks_green=None,
        )

    def test_unresolved_occ_author_fails_closed(self) -> None:
        assert not _author_identity_verdict(
            check_value="pytest tests/ -v",
            product_author_raw="jonah@omninode.ai",
            occ_author_raw=None,
            aliases={},
            sanctioned=frozenset(),
            ci_checks_green=None,
        )

    def test_carveout_fires_only_with_tautological_shape_and_green_ci(self) -> None:
        # Same identity on both sides — would otherwise FAIL — but the
        # tautological-existence-probe carve-out + confirmed-green CI PASSes.
        assert _author_identity_verdict(
            check_value=_SELF_BIND_CHECK_VALUE,
            product_author_raw="jonah@omninode.ai",
            occ_author_raw="jonah@omninode.ai",
            aliases={},
            sanctioned=frozenset(),
            ci_checks_green=True,
        )

    def test_carveout_does_not_fire_without_green_ci(self) -> None:
        assert not _author_identity_verdict(
            check_value=_SELF_BIND_CHECK_VALUE,
            product_author_raw="jonah@omninode.ai",
            occ_author_raw="jonah@omninode.ai",
            aliases={},
            sanctioned=frozenset(),
            ci_checks_green=None,
        )

    def test_carveout_does_not_fire_without_tautological_shape(self) -> None:
        # A real command/test_passes check_value never gets the carve-out,
        # even with ci_checks_green=True — the carve-out is shape-matched.
        assert not _author_identity_verdict(
            check_value="pytest tests/ -v",
            product_author_raw="jonah@omninode.ai",
            occ_author_raw="jonah@omninode.ai",
            aliases={},
            sanctioned=frozenset(),
            ci_checks_green=True,
        )


class TestAuthorIdentityGateFlag:
    def test_flag_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(_ENV, raising=False)
        assert _author_identity_gate_enabled() is False

    def test_flag_enabled_by_truthy_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for value in ("1", "true", "True", "YES", "on"):
            monkeypatch.setenv(_ENV, value)
            assert _author_identity_gate_enabled() is True
        monkeypatch.setenv(_ENV, "0")
        assert _author_identity_gate_enabled() is False


# ---------------------------------------------------------------------------
# _receipt_passes_author_identity_gate — I/O wrapper against a stub probe
# ---------------------------------------------------------------------------


class _StubIdentityProbe:
    def __init__(
        self,
        *,
        product_author: str | None,
        occ_author: str | None,
        ci_green: bool | None = None,
    ) -> None:
        self._product_author = product_author
        self._occ_author = occ_author
        self._ci_green = ci_green

    def product_author(self, *, repo: str, commit_sha: str) -> str | None:
        return self._product_author

    def occ_author(self, *, rel_path: str) -> str | None:
        return self._occ_author

    def ci_checks_green(self, *, repo: str, pr_number: int) -> bool | None:
        return self._ci_green


def _self_bind_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "ticket_id": "OMN-14431",
        "check_value": _SELF_BIND_CHECK_VALUE,
        "commit_sha": "38c31de5233baf8f1fe96065a58a2444ae712d4e",
        "pr_number": 4523,
    }
    payload.update(overrides)
    return payload


class TestReceiptPassesAuthorIdentityGate:
    def test_missing_check_value_fails_closed(self) -> None:
        payload = _self_bind_payload()
        del payload["check_value"]
        probe = _StubIdentityProbe(product_author="a@x", occ_author="b@x")
        assert not _receipt_passes_author_identity_gate(
            payload, "rel/path", probe=probe, aliases={}, sanctioned=frozenset()
        )

    def test_no_repo_in_check_value_fails_closed(self) -> None:
        payload = _self_bind_payload(check_value="pytest tests/ -v")
        probe = _StubIdentityProbe(product_author="a@x", occ_author="b@x")
        assert not _receipt_passes_author_identity_gate(
            payload, "rel/path", probe=probe, aliases={}, sanctioned=frozenset()
        )

    def test_same_identity_rejected(self) -> None:
        payload = _self_bind_payload()
        probe = _StubIdentityProbe(
            product_author="jonah@omninode.ai", occ_author="jonah@omninode.ai"
        )
        assert not _receipt_passes_author_identity_gate(
            payload, "rel/path", probe=probe, aliases={}, sanctioned=frozenset()
        )

    def test_carveout_with_green_ci_passes(self) -> None:
        payload = _self_bind_payload()
        probe = _StubIdentityProbe(
            product_author="jonah@omninode.ai",
            occ_author="jonah@omninode.ai",
            ci_green=True,
        )
        assert _receipt_passes_author_identity_gate(
            payload, "rel/path", probe=probe, aliases={}, sanctioned=frozenset()
        )

    def test_distinct_identity_passes(self) -> None:
        payload = _self_bind_payload(check_value=_REAL_CHECK_CHECK_VALUE)
        probe = _StubIdentityProbe(
            product_author="alice@omninode.ai", occ_author="bob@omninode.ai"
        )
        assert _receipt_passes_author_identity_gate(
            payload, "rel/path", probe=probe, aliases={}, sanctioned=frozenset()
        )

    def test_sibling_repo_hint_resolves_repo_when_own_check_value_has_none(
        self,
    ) -> None:
        """A plain ``pytest`` check_value carries no --repo flag, but a
        sibling receipt proving the SAME commit_sha does — the hint lets the
        gate resolve the repo instead of failing closed on missing repo."""
        payload = _self_bind_payload(
            check_value="pytest tests/ -v",
            commit_sha="a" * 40,
        )
        probe = _StubIdentityProbe(
            product_author="alice@omninode.ai", occ_author="bob@omninode.ai"
        )
        hints = {"a" * 40: "omnimarket"}
        assert _receipt_passes_author_identity_gate(
            payload,
            "rel/path",
            probe=probe,
            aliases={},
            sanctioned=frozenset(),
            commit_repo_hints=hints,
        )

    def test_no_hint_for_this_commit_sha_still_fails_closed(self) -> None:
        payload = _self_bind_payload(
            check_value="pytest tests/ -v",
            commit_sha="a" * 40,
        )
        probe = _StubIdentityProbe(product_author="alice@x", occ_author="bob@x")
        # Hint map has an entry, but for a DIFFERENT commit_sha.
        hints = {"b" * 40: "omnimarket"}
        assert not _receipt_passes_author_identity_gate(
            payload,
            "rel/path",
            probe=probe,
            aliases={},
            sanctioned=frozenset(),
            commit_repo_hints=hints,
        )


class TestBuildCommitRepoHints:
    def test_maps_commit_sha_to_repo_from_sibling_with_repo_flag(self) -> None:
        payloads = [
            {"check_value": "pytest tests/ -v", "commit_sha": "a" * 40},
            {
                "check_value": "gh pr view 1 --repo OmniNode-ai/omnimarket --json state",
                "commit_sha": "a" * 40,
            },
        ]
        hints = _build_commit_repo_hints(payloads)
        assert hints == {"a" * 40: "omnimarket"}

    def test_no_repo_anywhere_yields_empty_map(self) -> None:
        payloads = [{"check_value": "pytest tests/ -v", "commit_sha": "a" * 40}]
        assert _build_commit_repo_hints(payloads) == {}

    def test_distinct_commit_shas_kept_separate(self) -> None:
        payloads = [
            {
                "check_value": "gh pr view 1 --repo OmniNode-ai/omnimarket --json state",
                "commit_sha": "a" * 40,
            },
            {
                "check_value": (
                    "gh pr view 2 --repo OmniNode-ai/onex_change_control --json state"
                ),
                "commit_sha": "b" * 40,
            },
        ]
        hints = _build_commit_repo_hints(payloads)
        assert hints == {"a" * 40: "omnimarket", "b" * 40: "onex_change_control"}

    def test_malformed_sibling_payload_ignored(self) -> None:
        payloads = [
            {"check_value": 12345, "commit_sha": "a" * 40},
            {"check_value": "gh pr view 1 --repo OmniNode-ai/x --json state"},
        ]
        assert _build_commit_repo_hints(payloads) == {}


# ---------------------------------------------------------------------------
# AuthorIdentitySubprocessProbe — real git-backed resolution
# ---------------------------------------------------------------------------


def _init_git_repo(path: Path, *, author_email: str, author_name: str = "test") -> str:
    """Init a real git repo at ``path`` with one commit; return its SHA."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", author_email], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", author_name], cwd=path, check=True)
    (path / "file.txt").write_text("content\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"],
        cwd=path,
        check=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return sha


def _add_receipt_commit(
    occ_repo: Path, rel_path: str, payload: dict[str, object], *, author_email: str
) -> None:
    subprocess.run(
        ["git", "config", "user.email", author_email], cwd=occ_repo, check=True
    )
    target = occ_repo / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(payload), encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=occ_repo, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "receipt"],
        cwd=occ_repo,
        check=True,
    )


class TestAuthorIdentitySubprocessProbe:
    def test_product_author_resolves_real_commit(self, tmp_path: Path) -> None:
        omni_home = tmp_path / "omni_home"
        product_repo = omni_home / "onex_change_control"
        sha = _init_git_repo(product_repo, author_email="jonah@omninode.ai")
        probe = AuthorIdentitySubprocessProbe(omni_home=omni_home)
        assert (
            probe.product_author(repo="onex_change_control", commit_sha=sha)
            == "jonah@omninode.ai"
        )

    def test_product_author_missing_repo_fails_closed(self, tmp_path: Path) -> None:
        omni_home = tmp_path / "omni_home"
        omni_home.mkdir()
        probe = AuthorIdentitySubprocessProbe(omni_home=omni_home)
        assert probe.product_author(repo="does-not-exist", commit_sha="a" * 40) is None

    def test_product_author_missing_commit_fails_closed(self, tmp_path: Path) -> None:
        omni_home = tmp_path / "omni_home"
        product_repo = omni_home / "some_repo"
        _init_git_repo(product_repo, author_email="jonah@omninode.ai")
        probe = AuthorIdentitySubprocessProbe(omni_home=omni_home)
        assert probe.product_author(repo="some_repo", commit_sha="f" * 40) is None

    def test_occ_author_resolves_commit_touching_path(self, tmp_path: Path) -> None:
        occ_repo = tmp_path / "onex_change_control"
        occ_repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=occ_repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=occ_repo, check=True)
        _add_receipt_commit(
            occ_repo,
            "drift/dod_receipts/OMN-9999/dod-run/command.yaml",
            {"status": "PASS"},
            author_email="codex@omninode.ai",
        )
        probe = AuthorIdentitySubprocessProbe(
            occ_repo_path=occ_repo, governance_ref="HEAD"
        )
        assert (
            probe.occ_author(
                rel_path="drift/dod_receipts/OMN-9999/dod-run/command.yaml"
            )
            == "codex@omninode.ai"
        )

    def test_occ_author_missing_repo_fails_closed(self, tmp_path: Path) -> None:
        probe = AuthorIdentitySubprocessProbe(
            occ_repo_path=tmp_path / "does-not-exist", governance_ref="HEAD"
        )
        assert (
            probe.occ_author(rel_path="drift/dod_receipts/OMN-1/a/command.yaml") is None
        )

    def test_ci_checks_green_unreachable_gh_fails_closed(self, tmp_path: Path) -> None:
        probe = AuthorIdentitySubprocessProbe(omni_home=tmp_path)
        # No real network/gh call is exercised in this environment beyond the
        # subprocess boundary; a nonexistent PR against a bogus repo returns a
        # non-zero exit (or a subprocess error), both fail-closed to non-True.
        result = probe.ci_checks_green(
            repo="omninode-nonexistent-repo-xyz", pr_number=1
        )
        assert result is not True


# ---------------------------------------------------------------------------
# End-to-end RED-to-GREEN: the real Done-mutation probe call site
# ---------------------------------------------------------------------------


def _self_bind_full_payload(ticket_id: str = "OMN-14431") -> dict[str, object]:
    """The real OMN-14431 self-bind receipt shape (loose + strict gates PASS)."""
    return {
        "schema_version": "1.0.0",
        "ticket_id": ticket_id,
        "evidence_item_id": "occ-self-bind-pr-4523",
        "check_type": "command",
        "check_value": _SELF_BIND_CHECK_VALUE,
        "status": "PASS",
        "run_timestamp": datetime.now(tz=UTC).isoformat(),
        "commit_sha": None,  # filled in by the test with the real product SHA
        "runner": "codex-merge-controller",
        "verifier": "codex-letter",
        "probe_command": _SELF_BIND_CHECK_VALUE,
        "probe_stdout": '{"number":4523,"state":"OPEN"}',
        "pr_number": 4523,
    }


class _NoNetworkIdentityProbe(AuthorIdentitySubprocessProbe):
    """Real git-backed ``product_author``/``occ_author`` resolution (no
    network), but ``ci_checks_green`` never makes a live ``gh`` call — kept
    deterministic (``None`` = not confirmed green) so these tests exercise
    the identity-comparison path itself without depending on live GitHub
    state. The carve-out path is exercised separately with an explicit stub
    that reports confirmed-green CI.
    """

    def ci_checks_green(self, *, repo: str, pr_number: int) -> bool | None:
        return None


class TestOccReceiptDetailAuthorIdentityGateEndToEnd:
    """Drives the real ``OccReceiptSubprocessProbe.occ_receipt_detail`` call
    site with a real ``AuthorIdentitySubprocessProbe`` against real temp git
    repos — the same call chain ``HandlerLinearTriage`` uses in production."""

    def _build_fixture(
        self, tmp_path: Path, *, product_author: str, occ_author: str
    ) -> tuple[Path, str]:
        omni_home = tmp_path / "omni_home"
        product_repo = omni_home / "onex_change_control"
        sha = _init_git_repo(product_repo, author_email=product_author)

        occ_repo = omni_home / "onex_change_control_governance"
        occ_repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=occ_repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=occ_repo, check=True)
        payload = _self_bind_full_payload()
        payload["commit_sha"] = sha
        _add_receipt_commit(
            occ_repo,
            "drift/dod_receipts/OMN-14431/occ-self-bind-pr-4523/command.yaml",
            payload,
            author_email=occ_author,
        )
        return occ_repo, sha

    def _no_network_probe(
        self, tmp_path: Path, occ_repo: Path
    ) -> OccReceiptSubprocessProbe:
        """The real probe chain, minus the live ``gh pr checks`` network call."""
        identity_probe = _NoNetworkIdentityProbe(
            omni_home=tmp_path / "omni_home",
            occ_repo_path=occ_repo,
            governance_ref="HEAD",
        )
        return OccReceiptSubprocessProbe(
            occ_repo_path=occ_repo,
            governance_ref="HEAD",
            author_identity_probe=identity_probe,
        )

    def test_red_same_identity_self_bind_passes_today_flag_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RED: this is today's live behavior (OMN-14431) — the self-bind
        receipt is accepted verbatim; the author-identity gate does not run
        when the flag is off."""
        monkeypatch.delenv(_ENV, raising=False)
        occ_repo, _sha = self._build_fixture(
            tmp_path,
            product_author="jonah@omninode.ai",
            occ_author="jonah@omninode.ai",
        )
        monkeypatch.setenv("OMNI_HOME", str(tmp_path / "omni_home"))
        probe = self._no_network_probe(tmp_path, occ_repo)
        assert probe.occ_receipt_detail(ticket_id="OMN-14431") == _occ_receipt_dir(
            "OMN-14431"
        )

    def test_green_same_identity_self_bind_rejected_flag_on(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GREEN: with the flag on, the identical git-author on both sides is
        detected and the same on-disk receipt is refused."""
        monkeypatch.setenv(_ENV, "1")
        occ_repo, _sha = self._build_fixture(
            tmp_path,
            product_author="jonah@omninode.ai",
            occ_author="jonah@omninode.ai",
        )
        monkeypatch.setenv("OMNI_HOME", str(tmp_path / "omni_home"))
        probe = self._no_network_probe(tmp_path, occ_repo)
        assert probe.occ_receipt_detail(ticket_id="OMN-14431") is None

    def test_flag_on_still_accepts_genuinely_independent_author(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Positive control: a distinct human author on the OCC side passes."""
        monkeypatch.setenv(_ENV, "1")
        occ_repo, _sha = self._build_fixture(
            tmp_path,
            product_author="jonah@omninode.ai",
            occ_author="reviewer@omninode.ai",
        )
        monkeypatch.setenv("OMNI_HOME", str(tmp_path / "omni_home"))
        probe = self._no_network_probe(tmp_path, occ_repo)
        assert probe.occ_receipt_detail(ticket_id="OMN-14431") == _occ_receipt_dir(
            "OMN-14431"
        )

    def test_flag_on_accepts_sanctioned_automation_identity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sanctioned-automation control: bot@omninode.ai is on the governed
        sanctioned-automation allowlist, so it PASSes even though the product
        author string is identical to it in this synthetic fixture."""
        monkeypatch.setenv(_ENV, "1")
        occ_repo, _sha = self._build_fixture(
            tmp_path,
            product_author="bot@omninode.ai",
            occ_author="bot@omninode.ai",
        )
        monkeypatch.setenv("OMNI_HOME", str(tmp_path / "omni_home"))
        probe = self._no_network_probe(tmp_path, occ_repo)
        assert probe.occ_receipt_detail(ticket_id="OMN-14431") == _occ_receipt_dir(
            "OMN-14431"
        )

    def test_flag_on_carveout_passes_with_injected_green_ci_probe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The narrow self-bind carve-out: same identity, tautological
        existence-probe shape, but an injected probe reports CI green — the
        receipt PASSes via the carve-out, not via identity independence."""
        monkeypatch.setenv(_ENV, "1")
        occ_repo, _sha = self._build_fixture(
            tmp_path,
            product_author="jonah@omninode.ai",
            occ_author="jonah@omninode.ai",
        )
        monkeypatch.setenv("OMNI_HOME", str(tmp_path / "omni_home"))

        class _GreenCiProbe:
            def product_author(self, *, repo: str, commit_sha: str) -> str | None:
                return "jonah@omninode.ai"

            def occ_author(self, *, rel_path: str) -> str | None:
                return "jonah@omninode.ai"

            def ci_checks_green(self, *, repo: str, pr_number: int) -> bool | None:
                return True

        probe = OccReceiptSubprocessProbe(
            occ_repo_path=occ_repo,
            governance_ref="HEAD",
            author_identity_probe=_GreenCiProbe(),
        )
        assert probe.occ_receipt_detail(ticket_id="OMN-14431") == _occ_receipt_dir(
            "OMN-14431"
        )

    def test_sibling_repo_hint_resolves_a_repo_less_receipt_end_to_end(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Real receipt corpora commonly have a plain ``pytest`` receipt
        (no --repo flag) alongside a ``gh pr view --repo ...`` receipt for the
        SAME commit_sha. The plain receipt alone would fail closed on missing
        repo; with its sibling present, the hint resolves the repo and the
        distinct-author verdict runs correctly."""
        monkeypatch.setenv(_ENV, "1")
        omni_home = tmp_path / "omni_home"
        product_repo = omni_home / "omnimarket"
        sha = _init_git_repo(product_repo, author_email="alice@omninode.ai")

        occ_repo = omni_home / "onex_change_control_governance"
        occ_repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=occ_repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=occ_repo, check=True)

        repo_less_payload = {
            "schema_version": "1.0.0",
            "ticket_id": "OMN-9001",
            "evidence_item_id": "dod-001",
            "check_type": "command",
            "check_value": "pytest tests/ -v",
            "status": "PASS",
            "run_timestamp": datetime.now(tz=UTC).isoformat(),
            "commit_sha": sha,
            "runner": "worker-a",
            "verifier": "worker-a",
            "probe_command": "pytest tests/ -v",
            "probe_stdout": "10 passed",
        }
        repo_named_payload = {
            "schema_version": "1.0.0",
            "ticket_id": "OMN-9001",
            "evidence_item_id": "dod-002",
            "check_type": "command",
            "check_value": "gh pr view 1 --repo OmniNode-ai/omnimarket --json state",
            "status": "PASS",
            "run_timestamp": datetime.now(tz=UTC).isoformat(),
            "commit_sha": sha,
            "runner": "worker-a",
            "verifier": "worker-a",
            "probe_command": "gh pr view 1 --repo OmniNode-ai/omnimarket --json state",
            "probe_stdout": '{"state":"OPEN"}',
            "pr_number": 1,
        }
        _add_receipt_commit(
            occ_repo,
            "drift/dod_receipts/OMN-9001/dod-001/command.yaml",
            repo_less_payload,
            author_email="bob@omninode.ai",
        )
        _add_receipt_commit(
            occ_repo,
            "drift/dod_receipts/OMN-9001/dod-002/command.yaml",
            repo_named_payload,
            author_email="bob@omninode.ai",
        )
        monkeypatch.setenv("OMNI_HOME", str(omni_home))
        probe = self._no_network_probe(tmp_path, occ_repo)
        # Distinct product (alice) vs occ (bob) authors -> PASS, resolved via
        # the dod-002 sibling's --repo hint for dod-001's bare commit_sha.
        assert probe.occ_receipt_detail(ticket_id="OMN-9001") == _occ_receipt_dir(
            "OMN-9001"
        )


def test_author_identity_probe_is_runtime_checkable_protocol() -> None:
    assert isinstance(AuthorIdentitySubprocessProbe(), AuthorIdentityProbe)
