# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""End-to-end unit tests for the single OCC companion producer (OMN-14285).

Proves the ONE converged producer (:class:`OccCompanionEmitter`) authors the
deterministic companion the gate consumes and rebinds the product PR, for BOTH
former failure classes, with all git/network I/O mocked. Replaces the two
adapter test suites (``test_adapter_occ_autobind`` / ``test_adapter_occ_contract``)
that the convergence retired.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from omnibase_core.validation.validator_occ_merge_eligibility import (
    EnumOccEligibilityReason,
    ModelOccEligibilityInput,
    validate_occ_merge_eligibility,
)
from omnibase_core.validation.validator_receipt_gate import (
    compute_contract_entry_sha256,
)

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
    OccCompanionEmitter,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    ADMISSIBILITY_VALIDATOR_CHECK_VALUE,
    ADMISSIBILITY_VALIDATOR_EVIDENCE_ID,
    render_companion_contract,
    render_downstream_receipt,
)

_MOD = "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter"

# OMN-15441: the REAL response shape of POST /repos/{o}/{r}/issues/{n}/labels —
# the issue's full label ARRAY. These surrogates previously returned ``{}``
# through a patched ``rest_json``, which hid the dict-only contract violation
# and kept the suite green against a call that raised on every live run.
_LABELS_ARRAY_RESPONSE: list[dict[str, object]] = [
    {"id": 1, "name": "occ:machine-minted"},
    {"id": 2, "name": "evidence"},
]


# ---------------------------------------------------------------------------
# OMN-15317 — this suite asserts the LEGACY (pr_existence) companion bytes
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _pin_legacy_check_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``OMNI_OCC_CHECK_BINDING=pr_existence`` for every test in this module.

    OMN-15317 flipped the producer default to ``content_bound``. These tests
    assert byte-for-byte properties of the legacy check shape and drive
    ``_emit_companion_sync`` WITHOUT injecting a RED-derivable diff or faking
    ``_execute_probe_raw``, so under the new default every one of them would
    (correctly) take the fail-closed ``skip:NO_RED_DERIVABLE_CHECK`` branch and
    assert nothing about the property it was written for.

    Pinning is explicit rather than implicit: the properties covered here
    (F-01 append-only, F-02 placeholder-clean, F-03 yamlfmt idempotence, F-04
    pre-existing-contract append, F-06/F-16 probe parity, F-17 suppression, the
    OMN-14793 lease) are binding-orthogonal. The CONTENT-BOUND default path is
    driven end to end in the same blocking gate by
    ``test_occ_autobind_contention_omn_15247.py``
    (``TestShippedDefaultIsContentBound`` / ``TestContentBoundChecks``), which
    injects the probe results instead of reaching the network.
    """
    monkeypatch.setenv("OMNI_OCC_CHECK_BINDING", "pr_existence")


class _FakeTempDir:
    """A ``tempfile.TemporaryDirectory`` stand-in yielding a fixed path.

    Unlike the real thing it does NOT delete on exit, so the emitted companion
    files remain on disk for the test to inspect.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def __enter__(self) -> str:
        return str(self._path)

    def __exit__(self, *_exc: object) -> bool:
        return False


# ---------------------------------------------------------------------------
# _run_git transport
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRunGit:
    def test_run_git_returns_stripped_stdout(self, tmp_path: Path) -> None:
        emitter = OccCompanionEmitter()
        result = emitter._run_git(["git", "--version"], cwd=str(tmp_path))
        assert result.startswith("git version")

    def test_run_git_raises_on_nonzero_exit(self, tmp_path: Path) -> None:
        emitter = OccCompanionEmitter()
        with pytest.raises(subprocess.CalledProcessError):
            emitter._run_git(
                ["git", "rev-parse", "--verify", "refs/heads/nonexistent-xyz"],
                cwd=str(tmp_path),
            )


# ---------------------------------------------------------------------------
# _open_or_sync_occ_pr — open-or-sync idempotency
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOpenOrSyncOccPr:
    def test_returns_existing_open_pr_without_creating(self) -> None:
        emitter = OccCompanionEmitter()
        label_calls: list[str] = []

        def fake_rest(method: str, path: str, *, body=None, token=None) -> dict:
            if "/search/issues" in path:
                return {"items": [{"number": 4242}]}
            raise AssertionError(f"unexpected POST/GET during sync: {method} {path}")

        # OMN-14893: the sync path also (idempotently) verifies the
        # occ:machine-minted provenance marker. OMN-15441: that POST returns a
        # label ARRAY, so it routes through ``rest_json_array`` and this
        # surrogate returns the real shape.
        def fake_rest_array(method: str, path: str, *, body=None, token=None) -> list:
            label_calls.append(path)
            assert method == "POST"
            # OMN-16071: `ci:ready` is applied with the marker so the companion's
            # CI wave can fire at all (the OMN-15731 label-gated pilot otherwise
            # skips `pre-commit`, which CI Summary fails closed on).
            assert body == {"labels": ["occ:machine-minted", "ci:ready"]}
            return _LABELS_ARRAY_RESPONSE

        with (
            patch(f"{_MOD}.rest_json", side_effect=fake_rest),
            patch(f"{_MOD}.rest_json_array", side_effect=fake_rest_array),
            patch(f"{_MOD}._resolve_github_token", return_value="fake-token"),
        ):
            result = emitter._open_or_sync_occ_pr(
                branch="auto/x-pr-1-occ-autobind",
                ticket="OMN-9999",
                repo="OmniNode-ai/omnimarket",
                pr_number=1,
            )
        assert result == 4242  # synced to the existing companion, no create
        assert label_calls == [
            "/repos/OmniNode-ai/onex_change_control/issues/4242/labels"
        ]

    def test_creates_on_default_branch_when_none_exists(self) -> None:
        emitter = OccCompanionEmitter()
        posted: dict = {}
        label_calls: list[tuple[str, dict]] = []

        def fake_rest(method: str, path: str, *, body=None, token=None) -> dict:
            if "/search/issues" in path:
                return {"items": []}
            if method == "GET":  # default-branch resolution
                return {"default_branch": "dev"}
            posted.update(body or {})
            return {"number": 77}

        # OMN-15441: label POST decodes an ARRAY, via ``rest_json_array``.
        def fake_rest_array(method: str, path: str, *, body=None, token=None) -> list:
            label_calls.append((path, body or {}))
            return _LABELS_ARRAY_RESPONSE

        with (
            patch(f"{_MOD}.rest_json", side_effect=fake_rest),
            patch(f"{_MOD}.rest_json_array", side_effect=fake_rest_array),
            patch(f"{_MOD}._resolve_github_token", return_value="fake-token"),
        ):
            result = emitter._open_or_sync_occ_pr(
                branch="auto/x-pr-1-occ-autobind",
                ticket="OMN-9999",
                repo="OmniNode-ai/omnimarket",
                pr_number=1,
            )
        assert result == 77
        assert posted["base"] == "dev"  # OCC default branch, never hardcoded main
        # OMN-14893 provenance marker: applied on create too (the OCC#4661
        # gap — this emitter never applied it before this fix).
        # OMN-16071: `ci:ready` lands in the same POST as the marker.
        assert label_calls == [
            (
                "/repos/OmniNode-ai/onex_change_control/issues/77/labels",
                {"labels": ["occ:machine-minted", "ci:ready"]},
            )
        ]

    def test_label_failure_is_swallowed_never_aborts_author(self) -> None:
        """Best-effort contract (OMN-14893): a label API hiccup must not fail the mint."""
        from omnimarket.github_api import GitHubApiError

        emitter = OccCompanionEmitter()

        def fake_rest(method: str, path: str, *, body=None, token=None) -> dict:
            if "/search/issues" in path:
                return {"items": [{"number": 4242}]}
            raise AssertionError(f"unexpected call: {method} {path}")

        def fake_rest_array(method: str, path: str, *, body=None, token=None) -> list:
            assert path.endswith("/labels")
            raise GitHubApiError("label API down", status_code=500)

        with (
            patch(f"{_MOD}.rest_json", side_effect=fake_rest),
            patch(f"{_MOD}.rest_json_array", side_effect=fake_rest_array),
            patch(f"{_MOD}._resolve_github_token", return_value="fake-token"),
        ):
            result = emitter._open_or_sync_occ_pr(
                branch="auto/x-pr-1-occ-autobind",
                ticket="OMN-9999",
                repo="OmniNode-ai/omnimarket",
                pr_number=1,
            )
        assert result == 4242  # mint still succeeds despite the label failure

    def test_raises_on_missing_number(self) -> None:
        emitter = OccCompanionEmitter()

        def fake_rest(method: str, path: str, *, body=None, token=None) -> dict:
            if "/search/issues" in path:
                return {"items": []}
            if method == "GET":
                return {"default_branch": "dev"}
            return {"html_url": "https://github.com/x"}

        with (
            patch(f"{_MOD}.rest_json", side_effect=fake_rest),
            patch(f"{_MOD}._resolve_github_token", return_value="fake-token"),
            pytest.raises(RuntimeError, match="unexpected number field"),
        ):
            emitter._open_or_sync_occ_pr(
                branch="auto/x-pr-1-occ-autobind",
                ticket="OMN-9999",
                repo="OmniNode-ai/omnimarket",
                pr_number=1,
            )


# ---------------------------------------------------------------------------
# _patch_evidence_source — idempotent product-PR rebind
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPatchEvidenceSource:
    def test_patches_when_unbound(self) -> None:
        emitter = OccCompanionEmitter()
        calls: list[tuple[str, str]] = []

        def fake_rest(method: str, path: str, *, body=None, token=None) -> dict:
            calls.append((method, path))
            return {}

        with (
            patch(f"{_MOD}.rest_json", side_effect=fake_rest),
            patch(f"{_MOD}._resolve_github_token", return_value="fake-token"),
        ):
            emitter._patch_evidence_source(
                repo="OmniNode-ai/omnimarket",
                pr_number=77,
                occ_pr_number=99,
                tickets=["OMN-9999"],
                existing_body="existing PR body",
            )
        assert ("PATCH", "/repos/OmniNode-ai/omnimarket/pulls/77") in calls

    def test_noop_when_body_already_canonical(self) -> None:
        emitter = OccCompanionEmitter()
        # First render the canonical body, then feed it back as existing_body.
        from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_stamp_authoring import (
            render_product_pr_body_with_occ_source,
        )

        canonical = render_product_pr_body_with_occ_source(
            "body", occ_pr_number=99, tickets=["OMN-9999"]
        )
        patched = False

        def fake_rest(method: str, path: str, *, body=None, token=None) -> dict:
            nonlocal patched
            if method == "PATCH":
                patched = True
            return {}

        with (
            patch(f"{_MOD}.rest_json", side_effect=fake_rest),
            patch(f"{_MOD}._resolve_github_token", return_value="fake-token"),
        ):
            emitter._patch_evidence_source(
                repo="OmniNode-ai/omnimarket",
                pr_number=77,
                occ_pr_number=99,
                tickets=["OMN-9999"],
                existing_body=canonical,
            )
        assert patched is False


# ---------------------------------------------------------------------------
# already-bound idempotency guard
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAlreadyBoundGuard:
    def test_already_bound_pr_is_noop(self) -> None:
        emitter = OccCompanionEmitter()
        bound_body = "context\n\nEvidence-Source: OCC#123\nEvidence-Ticket: OMN-9999\n"

        def fake_rest(method: str, path: str, *, body=None, token=None) -> dict:
            return {
                "body": bound_body,
                "title": "feat(OMN-9999): x",
                "head": {"sha": "a" * 40, "ref": "feature"},
                "state": "open",
            }

        with (
            patch(f"{_MOD}.rest_json", side_effect=fake_rest),
            patch(f"{_MOD}._resolve_github_token", return_value="fake-token"),
        ):
            result = emitter._emit_companion_sync("OmniNode-ai/omnimarket", 5, None)
        assert "no-op" in result
        assert "OCC#123" in result


# ---------------------------------------------------------------------------
# Full mutate flow — one producer authors the deterministic companion
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFullEmitFlow:
    def _run(self, emitter: OccCompanionEmitter, tmp_path: Path) -> tuple[str, Path]:
        """Drive _emit_companion_sync with a REAL temp clone dir and mocked I/O.

        git + the OCC-PR-open + product-PR-patch + probe are mocked; the contract
        + receipt rendering, file writes, and contract_sha256 rebind run for real
        so the emitted companion byte-shape is exercised end-to-end.
        """
        clone_root = tmp_path / "onex_change_control"
        git_calls: list[list[str]] = []
        patched: dict = {}

        def fake_rest(method: str, path: str, *, body=None, token=None) -> dict:
            if path.endswith("/pulls/321"):  # product PR GET
                return {
                    "body": "Implements the thing.",
                    "title": "feat(OMN-9999): the thing",
                    "head": {"sha": "b" * 40, "ref": "feature-branch"},
                    "state": "open",
                }
            if "/pulls/55" in path:  # OCC PR GET after open-or-sync
                return {"number": 55, "state": "open"}
            return {}

        def fake_run_git(argv: list[str], *, cwd: str) -> str:
            git_calls.append(argv)
            return "c" * 40 if "rev-parse" in argv else ""

        with (
            patch(f"{_MOD}.rest_json", side_effect=fake_rest),
            patch(f"{_MOD}._resolve_github_token", return_value="fake-token"),
            # OMN-14793: the single-producer lease is always-on; this driver
            # exercises the mint, so grant the lease and no-op the release. The
            # lease acquire/reject behaviour is proven directly in the OMN-14793
            # fixtures (test_occ_companion_emitter_friction_omn_14741.py) and the
            # helper unit tests (test_occ_git_transport_lease.py).
            patch(f"{_MOD}.acquire_occ_companion_lease", return_value=True),
            patch(f"{_MOD}.release_occ_companion_lease"),
            patch.object(emitter, "_run_git", side_effect=fake_run_git),
            patch.object(
                emitter,
                "_clone_and_branch",
                side_effect=lambda cd, *_a: cd.mkdir(parents=True),
            ),
            patch.object(emitter, "_open_or_sync_occ_pr", return_value=55),
            patch.object(emitter, "_observe_pr_probe", return_value=("{}", 0)),
            patch.object(
                emitter,
                "_patch_evidence_source",
                side_effect=lambda **kw: patched.update(kw),
            ),
            patch(
                f"{_MOD}.tempfile.TemporaryDirectory",
                return_value=_FakeTempDir(tmp_path),
            ),
        ):
            action = emitter._emit_companion_sync("OmniNode-ai/omnimarket", 321, None)
        return action, clone_root

    def test_every_declared_entry_receipt_carries_a_per_entry_hash(
        self, tmp_path: Path
    ) -> None:
        """SEAM (OMN-14425 x OMN-14418 x OMN-14650): declared => hashed.

        OMN-14418 requires every receipt bound to a DECLARED dod_evidence item to
        carry `contract_entry_sha256`. OMN-14650 appends the self-bind item
        (`occ-self-bind-pr-<n>`) to the contract's dod_evidence, so the self-bind
        receipt is now ALSO a declared item and MUST carry the per-entry hash —
        matching the proven merged path. Post-OMN-14650 every emitted receipt
        binds a declared item, so the invariant collapses to: declared => hashed,
        checked over EVERY receipt so a future receipt cannot reintroduce a gap.
        """
        emitter = OccCompanionEmitter()
        _action, clone_root = self._run(emitter, tmp_path)

        contract_data = yaml.safe_load(
            (clone_root / "contracts" / "OMN-9999.yaml").read_text()
        )
        declared = {item["id"] for item in (contract_data.get("dod_evidence") or [])}
        # existence probe + diff-scope check + OMN-14650 self-bind item.
        assert len(declared) >= 3
        assert "occ-self-bind-pr-55" in declared, (
            "OMN-14650: the self-bind item must be registered in the contract's "
            "dod_evidence or eligibility never evaluates the self-bind receipt"
        )

        receipts = sorted(
            (clone_root / "drift" / "dod_receipts" / "OMN-9999").rglob("command.yaml")
        )
        assert receipts

        for receipt in receipts:
            data = yaml.safe_load(receipt.read_text()) or {}
            item_id = data.get("evidence_item_id")
            has_hash = bool(data.get("contract_entry_sha256"))
            if item_id in declared:
                assert has_hash, (
                    f"{receipt.parent.name} binds DECLARED item {item_id!r} but "
                    "carries no contract_entry_sha256 — receipt born pre-rotted"
                )
            else:
                assert not has_hash, (
                    f"{receipt.parent.name} binds UNDECLARED item {item_id!r} but "
                    "carries a fabricated contract_entry_sha256"
                )

    def test_authors_contract_and_receipts_and_rebinds(self, tmp_path: Path) -> None:
        emitter = OccCompanionEmitter()
        action, clone_root = self._run(emitter, tmp_path)

        # Contract authored for the gate-extracted ticket.
        contract = clone_root / "contracts" / "OMN-9999.yaml"
        assert contract.is_file()
        assert 'ticket_id: "OMN-9999"' in contract.read_text()

        # Downstream + CI-check (OMN-14425) + self-bind receipts authored.
        receipts = list(
            (clone_root / "drift" / "dod_receipts" / "OMN-9999").rglob("*.yaml")
        )
        assert len(receipts) >= 3  # downstream + ci-check + occ-self-bind

        # Every receipt's contract_sha256 is rebound to the real digest (no PENDING).
        digest = __import__("hashlib").sha256(contract.read_bytes()).hexdigest()
        for r in receipts:
            text = r.read_text()
            assert "PENDING" not in text
            assert f'contract_sha256: "sha256:{digest}"' in text

        # The downstream receipt commit_sha is the product head, not the OCC head.
        downstream = (
            clone_root
            / "drift"
            / "dod_receipts"
            / "OMN-9999"
            / "dod-OmniNode-ai-omnimarket-pr-321"
            / "command.yaml"
        )
        assert downstream.is_file()
        assert 'commit_sha: "' + "b" * 40 + '"' in downstream.read_text()

        # OMN-14418 residual 3: the downstream receipt (bound to the declared
        # dod_evidence item) carries a genuine per-entry hash matching the
        # canonical hasher, imported — never re-implemented.
        contract_data = yaml.safe_load(contract.read_text())
        expected_entry = compute_contract_entry_sha256(
            contract_data, "dod-OmniNode-ai-omnimarket-pr-321"
        )
        assert f'contract_entry_sha256: "{expected_entry}"' in downstream.read_text()

        # OMN-14650: the self-bind receipt's evidence_item_id is now APPENDED to
        # the contract's dod_evidence, so it binds via the per-entry scheme — it
        # MUST carry a contract_entry_sha256 matching the canonical hasher for its
        # own (now declared) entry, exactly like the proven merged path.
        self_bind = next(r for r in receipts if "occ-self-bind" in r.parent.name)
        self_bind_text = self_bind.read_text()
        self_bind_id = self_bind.parent.name  # occ-self-bind-pr-55
        assert self_bind_id == "occ-self-bind-pr-55"
        expected_self_bind_entry = compute_contract_entry_sha256(
            contract_data, self_bind_id
        )
        assert f'contract_entry_sha256: "{expected_self_bind_entry}"' in self_bind_text
        # OMN-14650: the CI receipt backs the product-diff-scope substance item,
        # no longer the deadlocking source-CI-green `gh pr checks` probe.
        ci_check = (
            clone_root
            / "drift"
            / "dod_receipts"
            / "OMN-9999"
            / "dod-OmniNode-ai-omnimarket-pr-321-ci"
            / "command.yaml"
        )
        assert ci_check.is_file()
        ci_text = ci_check.read_text()
        # OMN-14741 F-06: the CI receipt records a concrete GraphQL diff-scope
        # probe (`gh pr view --json files`), not the REST-fragile `gh pr diff`.
        assert (
            'check_value: "gh pr view 321 --repo OmniNode-ai/omnimarket '
            '--json files"' in ci_text
        )
        assert 'evidence_item_id: "dod-OmniNode-ai-omnimarket-pr-321-ci"' in ci_text

        # OMN-15407: the downstream + CI items' ids embed the product PR
        # number, so OMN-15382 Rule B requires a LITERAL pin, not the
        # ${PR_NUMBER}/${REPO} placeholder — existence probe and diff-scope
        # `--json files` check (not `gh pr diff`/`gh pr checks`).
        contract_text = contract.read_text()
        assert (
            "gh pr view 321 --repo OmniNode-ai/omnimarket --json number,state"
            in contract_text
        )
        assert (
            "gh pr view 321 --repo OmniNode-ai/omnimarket --json files" in contract_text
        )
        assert "gh pr checks" not in contract_text
        assert "gh pr diff" not in contract_text
        assert 'id: "occ-self-bind-pr-55"' in contract_text
        # OMN-15382: the self-bind item's OWN check_value is the literal
        # OCC PR pin, NOT the ${PR_NUMBER}/${REPO} placeholder — a
        # placeholder-only self-bind is a NEW Rule B (per-item PR binding)
        # violation on every freshly-minted companion (see
        # self_bind_check_value's docstring).
        assert (
            'check_value: "gh pr view 55 --repo OmniNode-ai/onex_change_control '
            '--json number,state"' in contract_text
        )

        # Action reports the single-producer companion bind.
        assert "OCC#55" in action
        assert "OMN-9999" in action

    def test_emitted_companion_is_occ_merge_eligible(self, tmp_path: Path) -> None:
        """Golden proof: emitted files satisfy the real OCC eligibility validator.

        OMN-14650 regressed because the self-bind receipt was written but its
        evidence id was not declared in the contract, so the validator never saw
        the only receipt bound to the OCC companion PR and returned
        pr_ticket_mismatch. Exercise the real validator against the emitted
        contract/receipt tree so the auto/* path proves eligible end to end.
        """
        emitter = OccCompanionEmitter()
        _action, clone_root = self._run(emitter, tmp_path)

        snapshot = ModelOccEligibilityInput(
            repo="onex_change_control",
            pr_number=55,
            pr_title=(
                "evidence(OMN-9999): OCC Evidence-Source autobind for "
                "OmniNode-ai/omnimarket#321"
            ),
            pr_body="Autobind OCC evidence.\n\nEvidence-Ticket: OMN-9999\n",
            pr_branch="auto/omninode-ai-omnimarket-pr-321-occ-autobind",
            pr_commit_shas=("c" * 40,),
            pr_commit_texts=(
                "evidence(OMN-9999): autobind OmniNode-ai/omnimarket#321",
            ),
            occ_commit_sha="c" * 40,
            contracts_dir=clone_root / "contracts",
            receipts_dir=clone_root / "drift" / "dod_receipts",
        )

        result = validate_occ_merge_eligibility(snapshot)

        assert result.eligible is True, result.detail
        assert result.reason is EnumOccEligibilityReason.ELIGIBLE
        assert "OMN-9999:occ-self-bind-pr-55:command" in result.receipt_ids

    def test_absent_self_bind_declaration_is_ineligible_red_case(
        self, tmp_path: Path
    ) -> None:
        """RED against exists-but-wrong (OMN-14650): with the self-bind receipt
        WRITTEN but its id NOT appended to the contract's dod_evidence — the
        exact pre-fix behavior — the validator never inspects the only receipt
        bound to the OCC companion PR and the companion is INELIGIBLE with
        pr_ticket_mismatch. This asserts against the wrong-but-present tree
        (receipt file exists on disk) so it fails on pre-fix code and proves the
        GREEN case above is non-vacuous.
        """
        emitter = OccCompanionEmitter()
        # Neutralize ONLY the OMN-14650 fix (the contract-declaration append);
        # everything else — the self-bind RECEIPT write, the rebind — still runs,
        # reproducing the exact born-broken auto/* companion.
        with patch.object(emitter, "_append_self_bind_evidence"):
            _action, clone_root = self._run(emitter, tmp_path)

        # The self-bind receipt file IS present (exists-but-wrong)...
        self_bind = (
            clone_root
            / "drift"
            / "dod_receipts"
            / "OMN-9999"
            / "occ-self-bind-pr-55"
            / "command.yaml"
        )
        assert self_bind.is_file(), "self-bind receipt must still be written"
        # ...but its id is absent from the contract's dod_evidence, so the
        # validator cannot reach it.
        contract_data = yaml.safe_load(
            (clone_root / "contracts" / "OMN-9999.yaml").read_text()
        )
        declared = {item["id"] for item in contract_data["dod_evidence"]}
        assert "occ-self-bind-pr-55" not in declared

        snapshot = ModelOccEligibilityInput(
            repo="onex_change_control",
            pr_number=55,
            pr_title=(
                "evidence(OMN-9999): OCC Evidence-Source autobind for "
                "OmniNode-ai/omnimarket#321"
            ),
            pr_body="Autobind OCC evidence.\n\nEvidence-Ticket: OMN-9999\n",
            pr_branch="auto/omninode-ai-omnimarket-pr-321-occ-autobind",
            pr_commit_shas=("c" * 40,),
            pr_commit_texts=(
                "evidence(OMN-9999): autobind OmniNode-ai/omnimarket#321",
            ),
            occ_commit_sha="c" * 40,
            contracts_dir=clone_root / "contracts",
            receipts_dir=clone_root / "drift" / "dod_receipts",
        )

        result = validate_occ_merge_eligibility(snapshot)

        assert result.eligible is False
        assert result.reason is EnumOccEligibilityReason.PR_TICKET_MISMATCH, (
            result.detail
        )

    def test_create_and_autobind_route_same_core(self, tmp_path: Path) -> None:
        """Both public entry points drive the identical authoring core."""
        import asyncio

        emitter = OccCompanionEmitter()
        with patch.object(
            emitter, "_emit_companion_sync", return_value="core-ran"
        ) as core:
            r1 = asyncio.run(
                emitter.autobind_evidence_source("OmniNode-ai/omnimarket", 1, "OMN-1")
            )
            r2 = asyncio.run(
                emitter.create_occ_contract("OmniNode-ai/omnimarket", 1, "OMN-1")
            )
        assert r1 == r2 == "core-ran"
        assert core.call_count == 2


# ---------------------------------------------------------------------------
# OMN-15785 — the ticket-shared admissibility-validator receipt must be
# minted net-new-file-only. A live incident (2026-08-09) proved the producer
# unconditionally `write_text()`-ed
# `drift/dod_receipts/<ticket>/dod-occ-evidence-admissibility-validator/
# command.yaml` on EVERY companion mint. That path is keyed on
# ADMISSIBILITY_VALIDATOR_EVIDENCE_ID, a fixed constant shared by every
# companion for the SAME ticket (unlike the downstream/CI/self-bind receipts,
# whose ids embed the product PR / OCC PR number and so never collide across
# companions). OMN-15789 got two companions under one ticket — OCC#6264 for
# omnibase_core#1550, then OCC#6276 for omnibase_infra#2705 — and the second
# companion's Stage-1 write silently overwrote the first companion's
# already-merged receipt, tripping the OCC Append-Only Gate (hand-repaired at
# onex_change_control@6240bf817). Root cause: no existence check before the
# write. Fix: mirror node_occ_companion_compute's own `state.exists and
# state.merged` guard (OMN-15485, which fixed the identical defect on the
# sibling compute-oracle path but was never ported here) — mint the
# admissibility receipt ONLY when this ticket's contract did not already
# exist before this run's clone, never on the "contract pre-existed" path.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAdmissibilityReceiptNetNewFileOnly:
    """OMN-15785: a second companion for an already-companioned ticket must
    never rewrite the shared, ticket-scoped admissibility-validator receipt.
    """

    @staticmethod
    def _seed_first_companion(clone_dir: Path) -> str:
        """Write a merged-looking first companion for OMN-9999 into the fresh
        clone dir, exactly as it would look after OCC#N (the ticket's FIRST
        companion, for a different product PR) already merged to dev — the
        state a second companion's `_clone_and_branch` observes.
        """
        contract_path = clone_dir / "contracts" / "OMN-9999.yaml"
        contract_path.parent.mkdir(parents=True, exist_ok=True)
        contract_path.write_text(
            render_companion_contract(
                ticket_id="OMN-9999",
                repo="OmniNode-ai/omnibase_core",
                pr_number=1550,
                evidence_id="dod-OmniNode-ai-omnibase_core-pr-1550",
            ),
            encoding="utf-8",
        )
        merged_receipt_text = render_downstream_receipt(
            ticket_id="OMN-9999",
            evidence_id=ADMISSIBILITY_VALIDATOR_EVIDENCE_ID,
            pr_number=1550,
            repo="OmniNode-ai/omnibase_core",
            run_timestamp="2026-08-09T17:37:20.644407+00:00",
            commit_sha="a" * 40,
            branch="auto/omninode-ai-omnibase_core-pr-1550-occ-autobind",
            probe_command="gh pr view 1550 --repo OmniNode-ai/omnibase_core --json number,state",
            probe_stdout='{"number":1550,"state":"OPEN"}',
            exit_code=0,
            actual_output=(
                "PASS: OCC runner executes the declared check; "
                "probe is the live PR read."
            ),
            check_value=ADMISSIBILITY_VALIDATOR_CHECK_VALUE,
        )
        merged_receipt_dir = (
            clone_dir
            / "drift"
            / "dod_receipts"
            / "OMN-9999"
            / ADMISSIBILITY_VALIDATOR_EVIDENCE_ID
        )
        merged_receipt_dir.mkdir(parents=True, exist_ok=True)
        (merged_receipt_dir / "command.yaml").write_text(
            merged_receipt_text, encoding="utf-8"
        )
        return merged_receipt_text

    def _run_second_companion(
        self, emitter: OccCompanionEmitter, tmp_path: Path
    ) -> tuple[str, Path, str]:
        """Drive `_emit_companion_sync` for a SECOND product PR (#322) that
        cites the SAME ticket (OMN-9999) a first companion already bound,
        against a clone dir pre-seeded with that first companion's merged
        contract + admissibility receipt.
        """
        clone_root = tmp_path / "onex_change_control"
        merged_receipt_text = ""

        def fake_rest(method: str, path: str, *, body=None, token=None) -> dict:
            if path.endswith("/pulls/322"):
                return {
                    "body": "Implements the other thing.",
                    "title": "feat(OMN-9999): the other thing",
                    "head": {"sha": "d" * 40, "ref": "feature-branch-2"},
                    "state": "open",
                }
            if "/pulls/56" in path:
                return {"number": 56, "state": "open"}
            return {}

        def fake_run_git(argv: list[str], *, cwd: str) -> str:
            return "e" * 40 if "rev-parse" in argv else ""

        def seed_clone(cd: Path, *_a: object) -> None:
            nonlocal merged_receipt_text
            cd.mkdir(parents=True)
            merged_receipt_text = self._seed_first_companion(cd)

        with (
            patch(f"{_MOD}.rest_json", side_effect=fake_rest),
            patch(f"{_MOD}._resolve_github_token", return_value="fake-token"),
            patch(f"{_MOD}.acquire_occ_companion_lease", return_value=True),
            patch(f"{_MOD}.release_occ_companion_lease"),
            patch.object(emitter, "_run_git", side_effect=fake_run_git),
            patch.object(emitter, "_clone_and_branch", side_effect=seed_clone),
            patch.object(emitter, "_open_or_sync_occ_pr", return_value=56),
            patch.object(emitter, "_observe_pr_probe", return_value=("{}", 0)),
            patch.object(emitter, "_patch_evidence_source"),
            patch(
                f"{_MOD}.tempfile.TemporaryDirectory",
                return_value=_FakeTempDir(tmp_path),
            ),
        ):
            action = emitter._emit_companion_sync("OmniNode-ai/omnimarket", 322, None)
        return action, clone_root, merged_receipt_text

    def test_second_companion_never_overwrites_the_merged_admissibility_receipt(
        self, tmp_path: Path
    ) -> None:
        emitter = OccCompanionEmitter()
        _action, clone_root, merged_receipt_text = self._run_second_companion(
            emitter, tmp_path
        )

        receipt_path = (
            clone_root
            / "drift"
            / "dod_receipts"
            / "OMN-9999"
            / ADMISSIBILITY_VALIDATOR_EVIDENCE_ID
            / "command.yaml"
        )
        assert receipt_path.read_text() == merged_receipt_text, (
            "OMN-15785: a second companion for an already-companioned ticket "
            "rewrote the already-merged, ticket-shared admissibility receipt "
            "instead of leaving it byte-for-byte untouched — the exact "
            "OCC#6264 -> #6276 append-only violation (2026-08-09)."
        )
        # The rewrite this receipt suffered live also included a re-bind pass
        # (contract_sha256 / contract_entry_sha256 restamped to the new
        # contract state) — assert those fields specifically, since a partial
        # fix that stops the Stage-1 write but still runs `_rebind_receipts`
        # over this id would still mutate the merged file.
        assert 'commit_sha: "' + "a" * 40 + '"' in receipt_path.read_text()
        assert "d" * 40 not in receipt_path.read_text()
        assert "pr_number: 1550" in receipt_path.read_text()

        # This PR's OWN companion evidence still mints normally alongside the
        # untouched shared receipt — the guard suppresses ONLY the collision.
        own_downstream = (
            clone_root
            / "drift"
            / "dod_receipts"
            / "OMN-9999"
            / "dod-OmniNode-ai-omnimarket-pr-322"
            / "command.yaml"
        )
        assert own_downstream.is_file()
        assert 'commit_sha: "' + "d" * 40 + '"' in own_downstream.read_text()

    def test_first_companion_for_a_ticket_still_mints_the_admissibility_receipt(
        self, tmp_path: Path
    ) -> None:
        """Normal mint (contract does not pre-exist) is unchanged: (b)."""
        emitter = OccCompanionEmitter()
        _action, clone_root = TestFullEmitFlow()._run(emitter, tmp_path)

        receipt_path = (
            clone_root
            / "drift"
            / "dod_receipts"
            / "OMN-9999"
            / ADMISSIBILITY_VALIDATOR_EVIDENCE_ID
            / "command.yaml"
        )
        assert receipt_path.is_file(), (
            "a ticket's FIRST companion must still mint the admissibility "
            "receipt net-new — the OMN-15785 guard must not suppress the "
            "ordinary case, only the same-ticket collision"
        )
        assert (
            'check_value: "uv run pytest tests/test_evidence_admissibility.py -q"'
            in (receipt_path.read_text())
            or "uv run pytest tests/test_evidence_admissibility.py -q"
            in (receipt_path.read_text())
        )
        assert "PENDING" not in receipt_path.read_text()


# ---------------------------------------------------------------------------
# OMN-14418 residual 3 — append-stability: a later append to the contract
# must NOT invalidate an already-minted downstream receipt's binding. This is
# the whole point of the fix: prove the receipt SURVIVES an append, where the
# legacy whole-file contract_sha256 does not.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAppendStability:
    def test_entry_hash_survives_append_whole_file_hash_does_not(
        self, tmp_path: Path
    ) -> None:
        emitter = OccCompanionEmitter()
        _action, clone_root = TestFullEmitFlow()._run(emitter, tmp_path)

        contract_path = clone_root / "contracts" / "OMN-9999.yaml"
        downstream_path = (
            clone_root
            / "drift"
            / "dod_receipts"
            / "OMN-9999"
            / "dod-OmniNode-ai-omnimarket-pr-321"
            / "command.yaml"
        )
        receipt_before = yaml.safe_load(downstream_path.read_text())

        # RED (pre-OMN-14418-R3 shape): a receipt that only ever bound the
        # legacy whole-file contract_sha256 has nothing that survives an
        # append — its ONLY binding goes stale the moment the contract grows.
        # This receipt (rendered by the current, fixed producer) instead
        # carries contract_entry_sha256; assert it is actually present before
        # testing survival, so a regression that drops the field back to None
        # fails this test loudly rather than passing vacuously.
        assert receipt_before["contract_entry_sha256"] is not None

        # Mutate the contract: append an unrelated dod_evidence item —
        # simulating a later independent append (e.g. any future evidence
        # requirement added to this ticket).
        contract_data = yaml.safe_load(contract_path.read_text())
        contract_data["dod_evidence"].append(
            {
                "id": "dod-OmniNode-ai-omnimarket-pr-321-later",
                "description": "unrelated later-appended check",
                "source": "generated",
                "checks": [
                    {
                        "check_type": "command",
                        "check_value": "gh pr checks 321 --repo OmniNode-ai/omnimarket",
                    }
                ],
            }
        )
        contract_path.write_text(yaml.safe_dump(contract_data, sort_keys=False))

        # GREEN: the per-entry hash for the ORIGINAL entry is unchanged by
        # the append — recomputing it against the now-appended contract
        # yields the same digest the receipt already carries.
        appended_contract_data = yaml.safe_load(contract_path.read_text())
        recomputed_entry = compute_contract_entry_sha256(
            appended_contract_data, "dod-OmniNode-ai-omnimarket-pr-321"
        )
        assert receipt_before["contract_entry_sha256"] == recomputed_entry

        # Contrast: the legacy whole-file hash DOES go stale on the same
        # append — this is the rot OMN-14411/OMN-14418 residual 3 describe.
        whole_file_after = f"sha256:{__import__('hashlib').sha256(contract_path.read_bytes()).hexdigest()}"
        assert receipt_before["contract_sha256"] != whole_file_after


# ---------------------------------------------------------------------------
# _resolve_github_token — OMN-14893 auth-mode switch (pat default / app opt-in)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestResolveGithubTokenAuthMode:
    def test_default_mode_is_pat_unchanged_behavior(self, monkeypatch) -> None:
        from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
            _resolve_github_token,
        )

        monkeypatch.delenv("OMNI_OCC_GITHUB_AUTH_MODE", raising=False)
        with (
            patch(f"{_MOD}.contract_secret_ref", return_value="GITHUB_TOKEN"),
            patch(f"{_MOD}.resolve_api_key") as mock_resolve,
        ):
            from pydantic import SecretStr

            mock_resolve.return_value = SecretStr("ghp_humanpat")
            token = _resolve_github_token()
        assert token == "ghp_humanpat"

    def test_app_mode_routes_through_app_auth_never_touches_pat(
        self, monkeypatch
    ) -> None:
        from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
            _resolve_github_token,
        )

        monkeypatch.setenv("OMNI_OCC_GITHUB_AUTH_MODE", "app")
        with (
            patch(
                f"{_MOD}.resolve_app_installation_token_from_contract",
                return_value="ghs_appminted",
            ) as mock_app_resolve,
            patch(f"{_MOD}.resolve_api_key") as mock_pat_resolve,
        ):
            token = _resolve_github_token()
        assert token == "ghs_appminted"
        mock_app_resolve.assert_called_once()
        # The pat-mode resolver is never touched at all in this branch.
        mock_pat_resolve.assert_not_called()

    def test_unknown_mode_raises(self, monkeypatch) -> None:
        from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
            _resolve_github_token,
        )

        monkeypatch.setenv("OMNI_OCC_GITHUB_AUTH_MODE", "bogus")
        with pytest.raises(RuntimeError, match="not a recognized OCC"):
            _resolve_github_token()

    def test_app_mode_credential_missing_propagates_no_pat_fallback(
        self, monkeypatch
    ) -> None:
        """A missing app credential in app mode must never fall back to the PAT."""
        from omnimarket.github_app_auth import GitHubAppCredentialMissingError
        from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
            _resolve_github_token,
        )

        monkeypatch.setenv("OMNI_OCC_GITHUB_AUTH_MODE", "app")
        with (
            patch(
                f"{_MOD}.resolve_app_installation_token_from_contract",
                side_effect=GitHubAppCredentialMissingError(
                    "ONEXBOT_OCC_APP_ID missing"
                ),
            ),
            patch(f"{_MOD}.resolve_api_key") as mock_pat_resolve,
            pytest.raises(GitHubAppCredentialMissingError, match="ONEXBOT_OCC_APP_ID"),
        ):
            _resolve_github_token()
        mock_pat_resolve.assert_not_called()
