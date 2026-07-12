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
from omnibase_core.validation.validator_receipt_gate import (
    compute_contract_entry_sha256,
)

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
    OccCompanionEmitter,
)

_MOD = "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter"


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

        def fake_rest(method: str, path: str, *, body=None, token=None) -> dict:
            if "/search/issues" in path:
                return {"items": [{"number": 4242}]}
            raise AssertionError(f"unexpected POST/GET during sync: {method} {path}")

        with (
            patch(f"{_MOD}.rest_json", side_effect=fake_rest),
            patch(f"{_MOD}._resolve_github_token", return_value="fake-token"),
        ):
            result = emitter._open_or_sync_occ_pr(
                branch="auto/x-pr-1-occ-autobind",
                ticket="OMN-9999",
                repo="OmniNode-ai/omnimarket",
                pr_number=1,
            )
        assert result == 4242  # synced to the existing companion, no create

    def test_creates_on_default_branch_when_none_exists(self) -> None:
        emitter = OccCompanionEmitter()
        posted: dict = {}

        def fake_rest(method: str, path: str, *, body=None, token=None) -> dict:
            if "/search/issues" in path:
                return {"items": []}
            if method == "GET":  # default-branch resolution
                return {"default_branch": "dev"}
            posted.update(body or {})
            return {"number": 77}

        with (
            patch(f"{_MOD}.rest_json", side_effect=fake_rest),
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
            return "occhead1234" if "rev-parse" in argv else ""

        with (
            patch(f"{_MOD}.rest_json", side_effect=fake_rest),
            patch(f"{_MOD}._resolve_github_token", return_value="fake-token"),
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

    def test_authors_contract_and_receipts_and_rebinds(self, tmp_path: Path) -> None:
        emitter = OccCompanionEmitter()
        action, clone_root = self._run(emitter, tmp_path)

        # Contract authored for the gate-extracted ticket.
        contract = clone_root / "contracts" / "OMN-9999.yaml"
        assert contract.is_file()
        assert 'ticket_id: "OMN-9999"' in contract.read_text()

        # Downstream + self-bind receipts authored under the ticket.
        receipts = list(
            (clone_root / "drift" / "dod_receipts" / "OMN-9999").rglob("*.yaml")
        )
        assert len(receipts) >= 2  # downstream + occ-self-bind

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

        # The self-bind receipt has NO declared dod_evidence counterpart, so
        # it must never carry a fabricated contract_entry_sha256.
        self_bind = next(r for r in receipts if "occ-self-bind" in r.parent.name)
        assert "contract_entry_sha256" not in self_bind.read_text()

        # Action reports the single-producer companion bind.
        assert "OCC#55" in action
        assert "OMN-9999" in action

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

        # Mutate the contract: append a second, unrelated dod_evidence item —
        # simulating a later independent append (e.g. OMN-14425's CI-check
        # item, or any future evidence requirement added to this ticket).
        contract_data = yaml.safe_load(contract_path.read_text())
        contract_data["dod_evidence"].append(
            {
                "id": "dod-OmniNode-ai-omnimarket-pr-321-ci",
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
