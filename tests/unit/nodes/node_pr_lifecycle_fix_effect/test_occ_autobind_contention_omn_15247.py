# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15247 golden tests against the LIVE OCC companion producer.

These drive the REAL :class:`OccCompanionEmitter` — the artifact that actually
mints companions on the merge sweep — not the unwired compute oracle. Two
defects are covered:

* **Contention.** The producer opened a SECOND companion for a ticket that
  already had an open hand-authored one, and the machine's shape landed
  (OMN-15229 OCC#5091 over #5089; OMN-15218 #5108 over #5107; OMN-15232 #5118
  over #5115). The displacement is unrecoverable: once the hollow contract
  merges, OCC is append-only and ``validator_occ_merge_eligibility`` rejects the
  repair (``pr_ticket_mismatch`` with ``missing_contracts: []``).
* **Non-falsifiable generated checks.** The CONTRACT's declared
  ``check_value`` — the string the OCC contract-compliance runner actually
  executes — is ``gh pr view ${PR_NUMBER} --repo ${REPO} --json number,state``,
  which exits 0 for ANY PR that exists in any state with any diff. OMN-14619
  built a content-read derivation but only ever landed it in a RECEIPT's
  ``check_value``, which the runner does not execute. Every content-bound
  assertion below is therefore made on the **contract**, never the receipt.

Defer-on-contention is ALWAYS ON — no env var, no constructor argument (see
``test_defer_is_mandatory_with_no_env_and_no_constructor_argument``).

OMN-15317 flipped the check-binding default from ``pr_existence`` to
``content_bound``: ``TestShippedDefaultIsContentBound`` now owns the
default-path assertion, and ``TestPrExistenceOptInIsByteIdentical`` keeps the
byte-identical legacy proof under an EXPLICIT ``pr_existence`` selection (it is
the reversal path and the shape the golden/parity fixtures assert), which is all
that assertion ever proved.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest
import yaml

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
    OccCompanionEmitter,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    ADMISSIBILITY_VALIDATOR_CHECK_VALUE,
    ci_dod_evidence_check_value,
    downstream_dod_evidence_check_value,
    downstream_receipt_public_check_value,
    render_companion_contract,
    render_downstream_receipt,
    self_bind_check_value,
)
from omnimarket.occ_content_probe import (
    build_content_read_check,
    is_yamlfmt_stable_check,
)
from omnimarket.occ_contention import (
    EnumCheckBinding,
    resolve_occ_producer_policy,
)

_MOD = "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter"

_HEAD_SHA = "b" * 40
_MERGE_BASE_SHA = "a" * 40
_OWN_BRANCH = "auto/omninode-ai-omnimarket-pr-321-occ-autobind"

# The canonical content-bound check the derivation produces for a real PR.
_CONTENT_BOUND_CHECK = build_content_read_check(
    repo="OmniNode-ai/omnimarket",
    path="src/omnimarket/handlers/handler_probe.py",
    kind="class",
    symbol="HandlerContentBoundProbe",
    head_sha=_HEAD_SHA,
)

# Mirror of onex_change_control/.yamlfmt (google/yamlfmt v0.21.0 config).
_OCC_YAMLFMT_CONF = (
    "formatter:\n"
    "  retain_line_breaks: true\n"
    "  max_line_length: 100\n"
    "  indent: 2\n"
    "  include_document_start: true\n"
    "  pad_line_comments: 2\n"
)

_CONTENT_BOUND_CLASS_PATCH = (
    "@@ -0,0 +1,3 @@\n+class HandlerContentBoundProbe:\n+    pass\n"
)


class _FakeTempDir:
    def __init__(self, path: Path) -> None:
        self._path = path

    def __enter__(self) -> str:
        return str(self._path)

    def __exit__(self, *_exc: object) -> bool:
        return False


def _default_pr_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "number": 321,
        "body": "Implements the thing.",
        "title": "feat(OMN-9999): the thing",
        "head": {"sha": _HEAD_SHA, "ref": "feature-branch"},
        "base": {"sha": "0" * 40, "repo": {"private": False}},
        "state": "open",
        "draft": False,
        "merged": False,
        "labels": [],
    }
    data.update(overrides)
    return data


class _Recorder:
    """Records every side-effecting call so a defer can be proven side-effect free."""

    def __init__(self) -> None:
        self.lease_calls: list[dict[str, object]] = []
        self.git_calls: list[list[str]] = []
        self.pr_open_calls: int = 0
        self.patch_evidence_calls: int = 0
        self.posted_comments: list[tuple[str, str]] = []


def _run_emit(
    emitter: OccCompanionEmitter,
    tmp_path: Path,
    *,
    pr_data: dict[str, object] | None = None,
    contending: dict[int, dict[str, object]] | None = None,
    pr_files: list[dict[str, object]] | None = None,
    content_at_ref: Callable[[str, str], str | None] | None = None,
    probe_exits: dict[str, int] | None = None,
    merge_base: str | None = _MERGE_BASE_SHA,
    product_repo: str = "OmniNode-ai/omnimarket",
) -> tuple[str, Path, _Recorder]:
    """Drive the REAL ``_emit_companion_sync`` with a temp clone + mocked I/O.

    ``contending`` maps an OCC PR number to ``{"ref": …, "labels": [...],
    "files": [...]}`` and is served through the search + pulls + files endpoints,
    so the contention probe exercises its real code path (search -> PR payload ->
    files -> ``companion_touches_ticket``), not a stubbed decision.

    ``probe_exits`` maps a ref substring to the exit code
    ``_execute_probe_raw`` should report for a probe pinned at that ref, so the
    mint-time RED/GREEN acceptance bar is driven for real.
    """
    clone_root = tmp_path / "onex_change_control"
    rec = _Recorder()
    resolved_pr_data = pr_data if pr_data is not None else _default_pr_data()
    contenders = contending or {}
    files = pr_files if pr_files is not None else []

    def fake_rest(method: str, path: str, *, body=None, token=None) -> dict:
        if path.endswith("/pulls/321"):
            return dict(resolved_pr_data)
        if "/search/issues" in path:
            return {"items": [{"number": n} for n in contenders]}
        if "/compare/" in path:
            return {"merge_base_commit": {"sha": merge_base}} if merge_base else {}
        if "/onex_change_control/pulls/" in path:
            number = int(path.rsplit("/", 1)[-1])
            entry = contenders.get(number, {})
            return {
                "head": {"ref": entry.get("ref", "")},
                "labels": [{"name": n} for n in entry.get("labels", [])],
            }
        if path.endswith("/comments") and method == "POST":
            rec.posted_comments.append((path, str((body or {}).get("body", ""))))
            return {"id": 1}
        if "/pulls/55" in path:
            return {"number": 55, "state": "open"}
        return {}

    def fake_rest_array(method: str, path: str, *, token=None) -> list[dict]:
        if "/onex_change_control/pulls/" in path and "/files" in path:
            number = int(path.split("/pulls/")[1].split("/")[0])
            entry = contenders.get(number, {})
            raw_files = entry.get("files", [])
            return [{"filename": f} for f in raw_files]  # type: ignore[union-attr]
        if "/files" in path:
            return files if "page=1" in path else []
        if path.endswith("/comments") or "/comments?" in path:
            return []
        return []

    def fake_run_git(argv: list[str], *, cwd: str) -> str:
        rec.git_calls.append(argv)
        if "rev-parse" in argv:
            return "c" * 40
        if "ls-remote" in argv:
            # OMN-15845: the base-freshness check runs before each force-push;
            # report the same base SHA fake_clone returns below so it always
            # reads as fresh — staleness itself is covered by
            # test_occ_companion_emitter_stale_base_omn_15845.py.
            return "0" * 40 + "\tHEAD\n"
        return ""

    def fake_clone(cd: Path, *_a: object) -> str:
        cd.mkdir(parents=True, exist_ok=True)
        return "0" * 40

    def fake_lease(**kwargs: object) -> bool:
        rec.lease_calls.append(kwargs)
        return True

    def fake_open_or_sync(**_kw: object) -> int:
        rec.pr_open_calls += 1
        return 55

    def fake_patch_evidence(**_kw: object) -> None:
        rec.patch_evidence_calls += 1

    def fake_probe_raw(command: str, *, token: str) -> tuple[str, int]:
        for ref, code in (probe_exits or {}).items():
            if f"?ref={ref}" in command:
                return ("1" if code == 0 else "0"), code
        return "", 1

    def fake_content(_o: str, _r: str, path: str, ref: str, _t: str) -> str | None:
        if content_at_ref is None:
            return None
        return content_at_ref(path, ref)

    with (
        patch(f"{_MOD}.rest_json", side_effect=fake_rest),
        patch(f"{_MOD}.rest_json_array", side_effect=fake_rest_array),
        patch(f"{_MOD}._resolve_github_token", return_value="fake-token"),
        patch(f"{_MOD}.acquire_occ_companion_lease", side_effect=fake_lease),
        patch(f"{_MOD}.release_occ_companion_lease", MagicMock()),
        patch.object(emitter, "_run_git", side_effect=fake_run_git),
        patch.object(emitter, "_clone_and_branch", side_effect=fake_clone),
        patch.object(emitter, "_open_or_sync_occ_pr", side_effect=fake_open_or_sync),
        patch.object(
            emitter, "_patch_evidence_source", side_effect=fake_patch_evidence
        ),
        patch.object(emitter, "_observe_pr_probe", return_value=("{}", 0)),
        patch.object(emitter, "_execute_probe_raw", side_effect=fake_probe_raw),
        patch.object(emitter, "_content_at_ref", side_effect=fake_content),
        patch(
            f"{_MOD}.tempfile.TemporaryDirectory",
            return_value=_FakeTempDir(tmp_path),
        ),
    ):
        action = emitter._emit_companion_sync(product_repo, 321, None)
    return action, clone_root, rec


def _contract_check_values(contract_path: Path) -> list[str]:
    data = yaml.safe_load(contract_path.read_text())
    return [
        check["check_value"]
        for item in (data.get("dod_evidence") or [])
        for check in (item.get("checks") or [])
        if isinstance(check.get("check_value"), str)
    ]


def _contract_check_values_by_item(contract_path: Path) -> list[tuple[str, str]]:
    """Same as :func:`_contract_check_values` but pairs each value with its
    owning item id (OMN-15382: the self-bind item is a deliberate,
    id-scoped exception to the placeholder-only rule).
    """
    data = yaml.safe_load(contract_path.read_text())
    return [
        (str(item.get("id", "")), check["check_value"])
        for item in (data.get("dod_evidence") or [])
        for check in (item.get("checks") or [])
        if isinstance(check.get("check_value"), str)
    ]


# A SHORT repo slug + path kept for fixture continuity across the OMN-15247
# lineage. Pre-foldproof-fix, this was the ONLY combination narrow enough for a
# double-quoted rendering to clear yamlfmt's column-100 fold (``o/r`` + ``x.py``
# = 7, the widest synthetic that survived quoted). Post-fix
# (``render_check_value_field`` auto-selects a literal block scalar whenever the
# quoted form would fold — see ``occ_content_probe.render_check_value_field``),
# EVERY realistic repo/path combination emits successfully; this fixture is
# retained only because several tests below reuse ``_stable_emit`` and do not
# depend on the repo slug being short. ``TestContentBoundChecks`` now proves the
# REAL, full-length repo/path shape (``OmniNode-ai/omnimarket``) also emits.
_STABLE_REPO = "o/r"
_STABLE_PATH = "x.py"


def _content_bound_fixture(
    *, path: str = _STABLE_PATH, symbol: str = "H"
) -> dict[str, object]:
    """A PR whose diff adds ``class <symbol>`` at ``path`` — RED-derivable."""
    return {
        "pr_files": [
            {
                "filename": path,
                "status": "modified",
                "patch": f"@@ -0,0 +1,3 @@\n+class {symbol}:\n+    pass\n",
            }
        ],
        "content_at_ref": lambda _path, ref: (
            f"class {symbol}:\n    pass\n" if ref == _HEAD_SHA else "# nothing yet\n"
        ),
        "probe_exits": {_HEAD_SHA: 0, _MERGE_BASE_SHA: 1},
    }


def _stable_emit(emitter: OccCompanionEmitter, tmp_path: Path, **kwargs: object):
    """Drive a mint against the short-slug repo so a stable check is derivable."""
    fixture = _content_bound_fixture()
    fixture.update(kwargs)
    return _run_emit(emitter, tmp_path, product_repo=_STABLE_REPO, **fixture)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The explicit pr_existence opt-in still reproduces the legacy bytes
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPrExistenceOptInIsByteIdentical:
    def test_pr_existence_produces_the_pre_omn_15247_contract_and_receipt_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OMN-15317: this was the DEFAULT-path assertion until the flip.

        It is retained verbatim under an explicit ``pr_existence`` selection
        because that binding is the reversal path and the shape the golden and
        consumer-gate parity fixtures assert; what changed is that nothing
        selects it implicitly any more. The default path is asserted by
        ``TestShippedDefaultIsContentBound``.
        """
        monkeypatch.setenv("OMNI_OCC_CHECK_BINDING", "pr_existence")
        emitter = OccCompanionEmitter()
        assert emitter._check_binding is EnumCheckBinding.PR_EXISTENCE

        # A fully RED-derivable PR is present and UNCONTENDED: under this
        # binding, not one emitted byte may change. (Contention is covered by
        # TestDeferOnContention — under contention there are no bytes at all.)
        fixture = _content_bound_fixture()
        action, clone_root, rec = _run_emit(
            emitter,
            tmp_path,
            **fixture,  # type: ignore[arg-type]
        )

        assert not action.startswith("skip:")
        assert rec.lease_calls, "the mint must still take the lease under defaults"
        assert rec.pr_open_calls == 1

        contract = clone_root / "contracts" / "OMN-9999.yaml"
        # OMN-15247 R21b: the pr_existence binding selects the GENERIC
        # (non-content-bound) vocabulary, which is the pre-R21 `gh pr view` family
        # restored. R21 had moved it to `gh api .../pulls/<n>/files ... | grep`,
        # which classifies ADMISSIBLE and proves nothing -- exit 0 for every PR on
        # GitHub that changes a file. These values are honest PROVENANCE: the OCC
        # runner reports them INERT/WARN, and the contract's admissibility comes
        # from the minted validator item appended after them.
        # OMN-15382/OMN-15407: BOTH the downstream binding item and the
        # self-bind item's check_values are now the literal, PR-pinned form,
        # not the ``hosted_safe_*`` placeholder — every item here has an id
        # that embeds a PR number (dod-...-pr-321, dod-...-pr-321-ci,
        # occ-self-bind-pr-55), so Rule B requires a literal pin on all three;
        # see downstream_dod_evidence_check_value's / self_bind_check_value's
        # docstrings for the rationale. Only the admissibility-validator item
        # (no PR number in its id) is unaffected.
        assert _contract_check_values(contract) == [
            downstream_dod_evidence_check_value(
                pr_number=321, repo="OmniNode-ai/omnimarket"
            ),
            ci_dod_evidence_check_value(pr_number=321, repo="OmniNode-ai/omnimarket"),
            ADMISSIBILITY_VALIDATOR_CHECK_VALUE,
            self_bind_check_value(
                occ_pr_number=55, occ_repo="OmniNode-ai/onex_change_control"
            ),
        ]
        receipt = yaml.safe_load(
            (
                clone_root
                / "drift"
                / "dod_receipts"
                / "OMN-9999"
                / "dod-OmniNode-ai-omnimarket-pr-321"
                / "command.yaml"
            ).read_text()
        )
        assert receipt["actual_output"] == (
            "PASS: Evidence-Source autobind for OMN-9999 from OmniNode-ai/omnimarket#321."
        )
        assert receipt["check_value"] == downstream_receipt_public_check_value(
            pr_number=321, repo="OmniNode-ai/omnimarket"
        )

    def test_unknown_mode_raises_at_construction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNI_OCC_CHECK_BINDING", "1")
        with pytest.raises(RuntimeError) as excinfo:
            OccCompanionEmitter()
        assert "OMNI_OCC_CHECK_BINDING" in str(excinfo.value)

    def test_no_contention_toggle_can_be_constructed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The retired opt-in cannot be re-introduced through either surface.

        A stale caller passing ``contention_policy=`` must fail loudly rather
        than being silently accepted, and a stale ``OMNI_OCC_CONTENTION_POLICY``
        left in a runtime env must not disable the guard.
        """
        with pytest.raises(TypeError):
            OccCompanionEmitter(contention_policy="observe")  # type: ignore[call-arg]
        monkeypatch.setenv("OMNI_OCC_CONTENTION_POLICY", "observe")
        emitter = OccCompanionEmitter()
        assert not hasattr(emitter, "_contention_policy")


# ---------------------------------------------------------------------------
# OMN-15317 — the shipped default is the FALSIFIABLE binding
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestShippedDefaultIsContentBound:
    """The flip, asserted on the artifact that runs, not on the enum.

    The two production sites construct ``OccCompanionEmitter()`` with no kwargs
    and set no env, so "what does the no-arg emitter write" IS the production
    question. Every test here drives the real ``_emit_companion_sync``.
    """

    def test_no_env_and_no_kwargs_resolves_content_bound(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OMNI_OCC_CHECK_BINDING", raising=False)
        assert (
            resolve_occ_producer_policy({}).check_binding
            is EnumCheckBinding.CONTENT_BOUND
        )
        assert OccCompanionEmitter()._check_binding is EnumCheckBinding.CONTENT_BOUND

    def test_the_default_mint_writes_a_content_bound_contract_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The CONTRACT check — the string the OCC runner executes — is bound.

        Asserted on ``contracts/<ticket>.yaml``, never on a receipt: the
        compliance runner executes contract check_values and ignores receipt
        ones, which is why OMN-14619's receipt-only content read was provenance
        rather than a gate.
        """
        monkeypatch.delenv("OMNI_OCC_CHECK_BINDING", raising=False)
        emitter = OccCompanionEmitter()
        action, clone_root, rec = _stable_emit(emitter, tmp_path)

        assert not action.startswith("skip:")
        assert rec.pr_open_calls == 1
        values = _contract_check_values(clone_root / "contracts" / "OMN-9999.yaml")
        assert values[0] == build_content_read_check(
            repo=_STABLE_REPO,
            path=_STABLE_PATH,
            kind="class",
            symbol="H",
            head_sha=_HEAD_SHA,
        )
        assert (
            "gh pr view ${PR_NUMBER} --repo ${REPO} --json number,state"
            not in (values[0])
        )

    def test_a_revert_shaped_pr_defers_under_the_default_and_minted_under_the_old(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RED/GREEN of the flip itself, on one input, driven end to end.

        A revert-shaped PR removes declarations and adds none, so no candidate is
        RED-derivable. Under the OLD default the producer still minted a
        companion whose contract check was ``gh pr view … --json number,state``
        — which exits 0 for any PR that exists, so the evidence PASSES for a
        revert. Under the OMN-15317 default the mint is refused outright, with
        zero side effects and a marked note on the product PR. There is no third
        outcome: the fallback-to-existence-probe path does not exist.
        """
        monkeypatch.delenv("OMNI_OCC_CHECK_BINDING", raising=False)
        revert_files: list[dict[str, object]] = [
            {
                "filename": _STABLE_PATH,
                "status": "modified",
                # Removal-only hunk: `extract_symbol_candidates` reads ADDED
                # declaration lines, so a revert yields zero candidates.
                "patch": "@@ -1,3 +0,0 @@\n-class H:\n-    pass\n",
            }
        ]

        legacy = OccCompanionEmitter(check_binding=EnumCheckBinding.PR_EXISTENCE)
        legacy_action, legacy_root, legacy_rec = _run_emit(
            legacy,
            tmp_path / "legacy",
            product_repo=_STABLE_REPO,
            pr_files=revert_files,
            content_at_ref=lambda _p, _r: "# nothing here\n",
            probe_exits={_HEAD_SHA: 1, _MERGE_BASE_SHA: 1},
        )
        assert not legacy_action.startswith("skip:")
        assert legacy_rec.pr_open_calls == 1
        # OMN-15407: literal, not the hosted_safe_* placeholder — see the Rule B
        # note above TestPrExistenceOptInIsByteIdentical's equivalent assertion.
        assert _contract_check_values(legacy_root / "contracts" / "OMN-9999.yaml")[
            0
        ] == downstream_dod_evidence_check_value(pr_number=321, repo=_STABLE_REPO)

        current = OccCompanionEmitter()
        action, clone_root, rec = _run_emit(
            current,
            tmp_path / "current",
            product_repo=_STABLE_REPO,
            pr_files=revert_files,
            content_at_ref=lambda _p, _r: "# nothing here\n",
            probe_exits={_HEAD_SHA: 1, _MERGE_BASE_SHA: 1},
        )
        assert action.startswith("skip:NO_RED_DERIVABLE_CHECK")
        assert rec.lease_calls == []
        assert rec.git_calls == []
        assert rec.pr_open_calls == 0
        assert rec.patch_evidence_calls == 0
        assert not clone_root.exists()
        assert any(
            "<!-- occ-autobind-no-red-derivable:321 -->" in body
            for _path, body in rec.posted_comments
        )

    def test_the_node_contract_declares_the_same_default_as_the_code(self) -> None:
        """The config overlay and the resolver must not drift.

        ``feedback_no_invisible_env_config_in_contract_overlays``: the declared
        default is what an operator reads; a contract that still advertises
        ``pr_existence`` while the code resolves ``content_bound`` would be a
        documented lie about production behavior.
        """
        contract = yaml.safe_load(
            (
                Path(__file__).resolve().parents[4]
                / "src"
                / "omnimarket"
                / "nodes"
                / "node_pr_lifecycle_fix_effect"
                / "contract.yaml"
            ).read_text()
        )
        declared = contract["config"]["OMNI_OCC_CHECK_BINDING"]["default"]
        assert declared == EnumCheckBinding.CONTENT_BOUND.value
        assert resolve_occ_producer_policy({}).check_binding is EnumCheckBinding(
            declared
        )


# ---------------------------------------------------------------------------
# Deliverable A — defer on contention
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDeferOnContention:
    def test_defer_is_mandatory_with_no_env_and_no_constructor_argument(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The load-bearing test: the SHIPPED producer defers, with no opt-in.

        Drives the real ``_emit_companion_sync`` on an emitter built exactly the
        way the two production sites build it (``OccCompanionEmitter()``, no
        kwargs) with no OMN-15247 env var set at all. A guard that only fires
        when an operator opts in is not a guard
        (``feedback_optional_input_means_the_check_does_not_exist``); this
        asserts the mint is suppressed on the default path.
        """
        monkeypatch.delenv("OMNI_OCC_CHECK_BINDING", raising=False)
        emitter = OccCompanionEmitter()
        action, clone_root, rec = _run_emit(
            emitter,
            tmp_path,
            contending={
                5115: {
                    "ref": "jonah/omn-9999-occ",
                    "labels": [],
                    "files": ["contracts/OMN-9999.yaml"],
                }
            },
        )
        assert action.startswith("skip:DEFER_HAND_AUTHORED")
        assert "OCC#5115" in action
        assert rec.lease_calls == []
        assert rec.git_calls == []
        assert rec.pr_open_calls == 0
        assert rec.patch_evidence_calls == 0
        assert not clone_root.exists()

    def test_defer_suppresses_the_mint_with_zero_side_effects(
        self, tmp_path: Path
    ) -> None:
        emitter = OccCompanionEmitter()
        action, clone_root, rec = _run_emit(
            emitter,
            tmp_path,
            contending={
                5115: {
                    "ref": "jonah/omn-9999-occ",
                    "labels": [],
                    "files": ["contracts/OMN-9999.yaml"],
                }
            },
        )
        assert action.startswith("skip:DEFER_HAND_AUTHORED")
        assert "OCC#5115" in action
        # Zero side effects: no lease, no git, no PR, no Evidence-Source patch.
        assert rec.lease_calls == []
        assert rec.git_calls == []
        assert rec.pr_open_calls == 0
        assert rec.patch_evidence_calls == 0
        assert not clone_root.exists()

    def test_defer_posts_one_idempotent_marked_note_on_the_contending_occ_pr(
        self, tmp_path: Path
    ) -> None:
        emitter = OccCompanionEmitter()
        _action, _clone, rec = _run_emit(
            emitter,
            tmp_path,
            contending={
                5115: {
                    "ref": "jonah/omn-9999-occ",
                    "labels": [],
                    "files": ["contracts/OMN-9999.yaml"],
                }
            },
        )
        assert len(rec.posted_comments) == 1
        path, body = rec.posted_comments[0]
        # On the OCC PR, not the product PR.
        assert "/onex_change_control/issues/5115/comments" in path
        assert "<!-- occ-autobind-deferred:OmniNode-ai/omnimarket#321 -->" in body

    def test_defer_does_not_defer_to_its_own_in_flight_branch(
        self, tmp_path: Path
    ) -> None:
        """Self-defer regression guard — a ``synchronize`` re-fire must still mint.

        Without the own-branch skip, the emitter's first mint would make every
        subsequent re-fire defer to itself, permanently wedging the producer.

        OMN-15317: driven through ``_stable_emit`` so the PR is RED-derivable.
        Under the content-bound default a candidate-less PR is refused for a
        different reason (NO_RED_DERIVABLE_CHECK), which would make "did the
        contention guard let this through" unobservable.
        """
        emitter = OccCompanionEmitter()
        action, clone_root, rec = _stable_emit(
            emitter,
            tmp_path,
            contending={
                5118: {
                    "ref": _OWN_BRANCH,
                    "labels": [],
                    "files": ["contracts/OMN-9999.yaml"],
                }
            },
        )
        assert not action.startswith("skip:")
        assert rec.pr_open_calls == 1
        assert (clone_root / "contracts" / "OMN-9999.yaml").is_file()

    def test_defer_does_not_defer_to_another_machine_companion(
        self, tmp_path: Path
    ) -> None:
        """Machine-vs-machine is the OMN-14793 lease's axis, not this one."""
        emitter = OccCompanionEmitter()
        action, _clone, rec = _stable_emit(
            emitter,
            tmp_path,
            contending={
                5091: {
                    "ref": "auto/omninode-ai-other-pr-9-occ-autobind",
                    "labels": ["occ:machine-minted"],
                    "files": ["contracts/OMN-9999.yaml"],
                }
            },
        )
        assert not action.startswith("skip:")
        assert rec.pr_open_calls == 1

    def test_defer_ignores_a_narrative_doc_that_merely_names_the_ticket(
        self, tmp_path: Path
    ) -> None:
        """The onex_change_control#5129 shape — a doc declares no dod_evidence."""
        emitter = OccCompanionEmitter()
        action, _clone, rec = _stable_emit(
            emitter,
            tmp_path,
            contending={
                5129: {
                    "ref": "jonah/omn-9999-evidence",
                    "labels": [],
                    "files": ["docs/evidence/OMN-9999/note.md"],
                }
            },
        )
        assert not action.startswith("skip:")
        assert rec.pr_open_calls == 1

    def test_defer_fails_toward_deferring_when_the_search_api_raises(
        self, tmp_path: Path
    ) -> None:
        emitter = OccCompanionEmitter()

        def exploding_rest(method: str, path: str, *, body=None, token=None) -> dict:
            if "/search/issues" in path:
                raise RuntimeError("search API 503")
            if path.endswith("/pulls/321"):
                return _default_pr_data()
            if path.endswith("/comments") and method == "POST":
                return {"id": 1}
            return {}

        with (
            patch(f"{_MOD}.rest_json", side_effect=exploding_rest),
            patch(f"{_MOD}.rest_json_array", return_value=[]),
            patch(f"{_MOD}._resolve_github_token", return_value="fake-token"),
            patch(f"{_MOD}.acquire_occ_companion_lease") as lease,
        ):
            action = emitter._emit_companion_sync("OmniNode-ai/omnimarket", 321, None)

        assert action.startswith("skip:DEFER_HAND_AUTHORED")
        assert "unknown" in action
        lease.assert_not_called()


# ---------------------------------------------------------------------------
# Deliverable B — content-bound contract checks
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestContentBoundChecks:
    """``content_bound`` is a complete, no-longer-inert mechanism (OMN-15247 foldproof).

    Measured against yamlfmt v0.21.0 with the real onex_change_control
    ``.yamlfmt``: a double-quoted plain scalar folds at the first space
    occurring past column 100. The rendered contract line is ``<8 spaces>
    check_value: "<value>"``, a 22-column prefix, so a QUOTED value is a
    fixpoint only when every space in it sits at index <= 78 — the SHORTEST
    possible content-bound read (``gh api repos/o/r/contents/x.py?ref=<7hex>
    --jq '.content' | base64 -d | grep -c 'class H'``, 90 chars) already
    crosses that. Pre-fix, the producer therefore emitted NOTHING and returned
    ``skip:NO_RED_DERIVABLE_CHECK`` for every real candidate — a fail-closed
    posture that was honest but made the binding an observable no-op.

    The fix (``occ_content_probe.render_check_value_field``) renders any value
    that would fold as a literal block scalar (``|-``) instead — MEASURED to
    be a yamlfmt fixpoint at ANY length, for the contract's indent-8
    ``check_value`` line and the receipt's indent-0 ``check_value`` /
    ``probe_command`` / ``actual_output`` lines alike (every realistic shape
    verified against the real binary in
    ``TestContentBoundSurvivesRealYamlfmt``). The mint-time ``accept=``
    column-length filter in ``_derive_content_bound_check`` is therefore
    removed: fold-safety is now a property of RENDERING, not of candidate
    selection, so every RED-derivable candidate is emitted regardless of its
    rendered length.
    """

    def test_a_real_repo_and_path_candidate_now_emits_the_content_bound_check(
        self, tmp_path: Path
    ) -> None:
        """OMN-15247 foldproof RED test #2: the emitter no longer skips.

        Drives the real full-length ``OmniNode-ai/omnimarket`` repo slug and a
        real handler path — the exact shape that was ALWAYS rejected pre-fix
        (``test_a_real_repo_and_path_candidate_is_never_emitted``, the prior
        name of this test). MUST FAIL pre-fix: the mint-time
        ``accept=is_yamlfmt_stable_check`` filter rejects this 201-char
        candidate outright, so the action is ``skip:NO_RED_DERIVABLE_CHECK``
        rather than a real mint.
        """
        emitter = OccCompanionEmitter(check_binding=EnumCheckBinding.CONTENT_BOUND)
        action, clone_root, rec = _run_emit(
            emitter,
            tmp_path,
            product_repo="OmniNode-ai/omnimarket",
            **_content_bound_fixture(  # type: ignore[arg-type]
                path="src/omnimarket/handlers/handler_probe.py",
                symbol="HandlerContentBoundProbe",
            ),
        )
        assert not action.startswith("skip:")
        assert rec.lease_calls, "a real mint must still take the lease"
        assert rec.pr_open_calls == 1
        values = _contract_check_values(clone_root / "contracts" / "OMN-9999.yaml")
        assert values[0] == _CONTENT_BOUND_CHECK

    def test_even_the_shortest_possible_candidate_also_emits(
        self, tmp_path: Path
    ) -> None:
        """Fold-safety no longer depends on path depth — every length now emits."""
        emitter = OccCompanionEmitter(check_binding=EnumCheckBinding.CONTENT_BOUND)
        action, clone_root, _rec = _stable_emit(emitter, tmp_path)
        assert not action.startswith("skip:")
        values = _contract_check_values(clone_root / "contracts" / "OMN-9999.yaml")
        assert values[0] == build_content_read_check(
            repo=_STABLE_REPO,
            path=_STABLE_PATH,
            kind="class",
            symbol="H",
            head_sha=_HEAD_SHA,
        )

    def test_the_pr_existence_binding_still_mints_on_the_same_pr(
        self, tmp_path: Path
    ) -> None:
        """The content-bound derivation runs in BOTH modes; only the
        ``pr_existence`` binding's declared check_value is unaffected by it.

        OMN-15317 moved this from "the default binding" to "the explicitly
        selected legacy binding" — the derivation running in both modes is what
        keeps the OFF state observable rather than absent.
        """
        _action, clone_root, rec = _stable_emit(
            OccCompanionEmitter(check_binding=EnumCheckBinding.PR_EXISTENCE), tmp_path
        )
        values = _contract_check_values(clone_root / "contracts" / "OMN-9999.yaml")
        # OMN-15407: literal, not the hosted_safe_* placeholder (Rule B).
        assert values[0] == downstream_dod_evidence_check_value(
            pr_number=321, repo=_STABLE_REPO
        )
        assert rec.pr_open_calls == 1

    def test_no_candidate_at_all_fails_closed_without_falling_back(
        self, tmp_path: Path
    ) -> None:
        """A silent fallback here is the exact behavior OMN-15247 files as a defect.

        Zero RED-derivable candidates (a README-only diff) is orthogonal to
        the fold fix — this must stay a fail-closed skip regardless.
        """
        emitter = OccCompanionEmitter(check_binding=EnumCheckBinding.CONTENT_BOUND)
        action, clone_root, rec = _run_emit(
            emitter,
            tmp_path,
            product_repo=_STABLE_REPO,
            pr_files=[{"filename": "README.md", "status": "modified", "patch": "+hi"}],
        )
        assert action.startswith("skip:NO_RED_DERIVABLE_CHECK")
        assert not clone_root.exists()
        assert rec.lease_calls == []

    def test_an_unresolvable_merge_base_fails_closed(self, tmp_path: Path) -> None:
        """Merge-base resolution failure is orthogonal to the fold fix."""
        emitter = OccCompanionEmitter(check_binding=EnumCheckBinding.CONTENT_BOUND)
        action, _clone, _rec = _stable_emit(emitter, tmp_path, merge_base=None)
        assert action.startswith("skip:NO_RED_DERIVABLE_CHECK")

    def test_no_red_derivable_posts_one_marked_note_on_the_product_pr(
        self, tmp_path: Path
    ) -> None:
        """Uses the genuine no-candidate fixture — ``_stable_emit`` now mints."""
        emitter = OccCompanionEmitter(check_binding=EnumCheckBinding.CONTENT_BOUND)
        _action, _clone, rec = _run_emit(
            emitter,
            tmp_path,
            product_repo=_STABLE_REPO,
            pr_files=[{"filename": "README.md", "status": "modified", "patch": "+hi"}],
        )
        assert len(rec.posted_comments) == 1
        path, body = rec.posted_comments[0]
        assert "/repos/o/r/issues/321/comments" in path
        assert "<!-- occ-autobind-no-red-derivable:321 -->" in body

    def test_private_product_repo_never_pins_the_private_repo_or_its_own_receipts(
        self, tmp_path: Path
    ) -> None:
        """OMN-15247 R21 replaced the old F-16 assertion; OMN-15407 updates it again.

        The old (pre-R21) test required the private-repo path to emit
        ``grep -q '^status: PASS$' $CONTRACT_REPO_DIR/drift/dod_receipts/...`` --
        a form the OMN-15309 predicate refuses UNCONDITIONALLY as
        INSIDE_OWN_DIFF, and the direct cause of the born-red companions on
        OCC#5406 / #5415 / #5418. That circular-grep prohibition still holds
        and is asserted below. What changed at OMN-15407: the placeholder form
        R21 introduced to satisfy "never dereference the private repo" is now
        ITSELF a Rule B violation (OMN-15382, live on onex_change_control dev
        since 06d4294e) for the downstream/CI items, whose ids embed the PR
        number -- a literal pin is safe here because the private repo's name
        is already committed verbatim in the item's own id, and ``gh pr view``
        is demoted to WARN by the OMN-15309 predicate regardless of literal vs.
        placeholder spelling (a private-repo 403/404 cannot newly BLOCK).
        """
        emitter = OccCompanionEmitter(check_binding=EnumCheckBinding.CONTENT_BOUND)
        action, clone_root, _rec = _stable_emit(
            emitter,
            tmp_path,
            pr_data=_default_pr_data(base={"sha": "0" * 40, "repo": {"private": True}}),
        )
        assert not action.startswith("skip:")
        pairs = _contract_check_values_by_item(
            clone_root / "contracts" / "OMN-9999.yaml"
        )
        values = [cv for _item_id, cv in pairs]
        assert values, "a private-repo companion must still declare checks"
        for item_id, cv in pairs:
            # No self-reference: the circular receipt grep this ticket removes.
            assert "dod_receipts" not in cv, cv
            assert "CONTRACT_REPO_DIR" not in cv, cv
            # OMN-15247 R21b: a literal/placeholder pin is required only of
            # values that NAME a repo/PR. The minted admissibility-validator
            # check is repo-independent -- it names neither, so there is
            # nothing to pin.
            if "repos/" in cv or " --repo " in cv:
                assert "${REPO}" not in cv, cv
                assert "${PR_NUMBER}" not in cv, cv
                if item_id.startswith("occ-self-bind-pr-"):
                    # Pins OCC's OWN (public onex_change_control) PR, never
                    # the private product repo this test is about.
                    continue
                assert _STABLE_REPO in cv, cv
                assert "321" in cv, cv
        # And the one admissible check IS present on the private path -- the whole
        # point of the fix, on the population that was born red three-for-three.
        assert "uv run pytest tests/test_evidence_admissibility.py -q" in values

    def test_every_generated_yaml_stays_yamlfmt_idempotent(
        self, tmp_path: Path
    ) -> None:
        """Whatever a binding mode emits must be a yamlfmt fixpoint."""
        yamlfmt = shutil.which("yamlfmt")
        if yamlfmt is None:
            pytest.skip("yamlfmt binary not available (installed in the CI gate)")

        for binding in (EnumCheckBinding.PR_EXISTENCE, EnumCheckBinding.CONTENT_BOUND):
            for private in (False, True):
                run_dir = tmp_path / f"run-{binding.value}-{private}"
                run_dir.mkdir()
                _action, clone_root, _rec = _stable_emit(
                    OccCompanionEmitter(check_binding=binding),
                    run_dir,
                    pr_data=_default_pr_data(
                        base={"sha": "0" * 40, "repo": {"private": private}}
                    ),
                )
                generated = (
                    sorted(clone_root.rglob("*.yaml")) if clone_root.exists() else []
                )
                if not generated:
                    continue  # fail-closed skip emitted nothing at all
                conf = run_dir / ".yamlfmt"
                conf.write_text(_OCC_YAMLFMT_CONF)
                result = subprocess.run(
                    [
                        yamlfmt,
                        "-lint",
                        "-conf",
                        str(conf),
                        *[str(p) for p in generated],
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                assert result.returncode == 0, (
                    f"{binding.value}/private={private} YAML is not a yamlfmt "
                    "fixpoint and would restale contract_sha256:\n"
                    f"{result.stdout}\n{result.stderr}"
                )


@pytest.mark.unit
class TestLockfileOnlyPrsMintUnderOmn16410:
    """OMN-16410: a uv.lock-only PR now mints instead of declining.

    Pre-fix, ``extract_symbol_candidates`` only ever proposed Python
    ``class``/``def`` candidates, so any PR whose diff touched nothing but
    ``uv.lock`` (the OMN-13902 sibling-lock-refresh bot's entire output shape)
    hit ``skip:NO_RED_DERIVABLE_CHECK`` unconditionally — live-reproduced on
    omnibase_infra#2848 with the bot's own decline comment: "no changed-file
    candidate could be proven RED against the merge base... Hand-authored
    evidence is required (OMN-15247)."
    """

    _LOCK_HEAD = (
        "[[package]]\n"
        'name = "omnibase-core"\n'
        'version = "0.46.9"\n'
        'source = { registry = "http://mirror.internal:3141/root/pypi/+simple/" }\n'
    )
    _LOCK_BASE = (
        "[[package]]\n"
        'name = "omnibase-core"\n'
        'version = "0.46.9"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
    )

    def _lockfile_fixture(self) -> dict[str, object]:
        return {
            "pr_files": [{"filename": "uv.lock", "status": "modified", "patch": None}],
            "content_at_ref": lambda _path, ref: (
                self._LOCK_HEAD if ref == _HEAD_SHA else self._LOCK_BASE
            ),
            "probe_exits": {_HEAD_SHA: 0, _MERGE_BASE_SHA: 1},
        }

    def test_uv_lock_only_diff_now_mints_a_content_bound_check(
        self, tmp_path: Path
    ) -> None:
        emitter = OccCompanionEmitter(check_binding=EnumCheckBinding.CONTENT_BOUND)
        action, clone_root, rec = _run_emit(
            emitter,
            tmp_path,
            product_repo=_STABLE_REPO,
            **self._lockfile_fixture(),  # type: ignore[arg-type]
        )
        assert not action.startswith("skip:"), action
        assert rec.lease_calls, "a real mint must still take the lease"
        values = _contract_check_values(clone_root / "contracts" / "OMN-9999.yaml")
        assert values, "no content-bound check landed in the contract"
        assert "mirror.internal:3141" in values[0]
        assert "grep -cF" in values[0]

    def test_uv_lock_diff_with_no_actual_content_delta_still_fails_closed(
        self, tmp_path: Path
    ) -> None:
        """No fabricated fallback: a lockfile PR whose content is byte-identical
        at head and base (e.g. a metadata-only touch) still declines."""
        emitter = OccCompanionEmitter(check_binding=EnumCheckBinding.CONTENT_BOUND)
        fixture = self._lockfile_fixture()
        fixture["content_at_ref"] = lambda _path, _ref: self._LOCK_BASE
        action, clone_root, rec = _run_emit(
            emitter,
            tmp_path,
            product_repo=_STABLE_REPO,
            **fixture,  # type: ignore[arg-type]
        )
        assert action.startswith("skip:NO_RED_DERIVABLE_CHECK")
        assert not clone_root.exists()
        assert rec.lease_calls == []


@pytest.mark.unit
class TestContentBoundRenderingSeam:
    """The rendering + consumer-gate wiring the emitter's guard currently gates off.

    ``render_companion_contract`` is what would carry a content-bound value into
    the CONTRACT (the artifact the compliance runner executes) — the OMN-14619
    gap, which only ever reached a RECEIPT. These assert the seam threads the
    value and that the FORM clears every consumer gate, so when the yamlfmt
    blocker is closed the remaining wiring is already proven.
    """

    @staticmethod
    def _contract_text() -> str:
        return render_companion_contract(
            ticket_id="OMN-9999",
            repo="OmniNode-ai/omnimarket",
            pr_number=321,
            evidence_id="dod-OmniNode-ai-omnimarket-pr-321",
            downstream_check_value=_CONTENT_BOUND_CHECK,
        )

    def test_the_contract_declares_the_content_read_not_the_existence_probe(
        self, tmp_path: Path
    ) -> None:
        contract = tmp_path / "OMN-9999.yaml"
        contract.write_text(self._contract_text())
        values = _contract_check_values(contract)
        assert values[0] == _CONTENT_BOUND_CHECK
        # The OMN-14409 diff-scope item is untouched by the content-bound
        # override — removing it is not in scope. OMN-15407: it now renders
        # literally pinned (Rule B), not the hosted_safe_* placeholder.
        assert values[1] == ci_dod_evidence_check_value(
            pr_number=321, repo="OmniNode-ai/omnimarket"
        )

    def test_the_content_bound_check_value_survives_real_yamlfmt_byte_identical(
        self, tmp_path: Path
    ) -> None:
        """OMN-15247 foldproof RED test #1.

        Renders a REAL content-bound candidate through the CURRENT emitter
        rendering seam (:func:`render_companion_contract`), runs the ACTUAL
        pinned yamlfmt v0.21.0 binary against the resulting contract file
        (mirroring the real onex_change_control ``.yamlfmt``), and asserts the
        file's bytes are unchanged and the parsed ``check_value`` byte-equals
        the intended command. MUST FAIL pre-fix: the emitter's quoted plain
        scalar rendering folds this 201-char value at the first space past
        column 100 (measured, ``is_yamlfmt_stable_check`` returns False for it
        at indent 8), which would restale ``contract_sha256`` on the hosted
        Pre-commit run.
        """
        yamlfmt = shutil.which("yamlfmt")
        if yamlfmt is None:
            pytest.skip("yamlfmt binary not available (installed in the CI gate)")

        contract = tmp_path / "OMN-9999.yaml"
        contract.write_text(self._contract_text())
        before = contract.read_bytes()

        conf = tmp_path / ".yamlfmt"
        conf.write_text(_OCC_YAMLFMT_CONF)
        result = subprocess.run(
            [yamlfmt, "-conf", str(conf), str(contract)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        after = contract.read_bytes()

        assert after == before, (
            "yamlfmt rewrote the rendered contract — the check_value line is "
            "not a formatter fixpoint, which would restale contract_sha256:\n"
            f"before:\n{before.decode()}\nafter:\n{after.decode()}"
        )
        values = _contract_check_values(contract)
        assert values[0] == _CONTENT_BOUND_CHECK

    def test_the_receipt_carries_the_same_string_and_the_red_derivation(self) -> None:
        receipt = yaml.safe_load(
            render_downstream_receipt(
                ticket_id="OMN-9999",
                evidence_id="dod-OmniNode-ai-omnimarket-pr-321",
                pr_number=321,
                repo="OmniNode-ai/omnimarket",
                run_timestamp="2026-07-27T00:00:00Z",
                commit_sha=_HEAD_SHA,
                branch=_OWN_BRANCH,
                probe_command=_CONTENT_BOUND_CHECK,
                probe_stdout='{"green_exit":0,"red_exit":1}',
                exit_code=0,
                check_value=_CONTENT_BOUND_CHECK,
                actual_output="PASS: content-bound probe GREEN at head, RED at base.",
            )
        )
        assert receipt["check_value"] == _CONTENT_BOUND_CHECK
        assert receipt["actual_output"].startswith("PASS: content-bound probe")
        # No schema key was invented — ModelDodReceipt is extra="forbid"/frozen.
        assert "red_derivation" not in receipt


@pytest.mark.unit
class TestYamlfmtStabilityPredicateMatchesTheRealFormatter:
    """The predicate must never drift from the formatter it models.

    ``is_yamlfmt_stable_check`` encodes a MEASURED rule about yamlfmt v0.21.0.
    A re-implemented predicate that silently diverges from the real binary is
    exactly the "two independent unit suites" failure CLAUDE.md forbids, so
    every case below is adjudicated by running the actual formatter.
    """

    @staticmethod
    def _real_yamlfmt_is_fixpoint(yamlfmt: str, tmp_path: Path, value: str) -> bool:
        conf = tmp_path / ".yamlfmt"
        conf.write_text(_OCC_YAMLFMT_CONF)
        target = tmp_path / "probe.yaml"
        target.write_text(
            "---\n"
            "dod_evidence:\n"
            '  - id: "x"\n'
            "    checks:\n"
            '      - check_type: "command"\n'
            f'        check_value: "{value}"\n'
        )
        before = target.read_text()
        subprocess.run(
            [yamlfmt, "-conf", str(conf), str(target)],
            capture_output=True,
            check=False,
        )
        return target.read_text() == before

    @pytest.mark.parametrize(
        "value",
        [
            # Today's public existence probe — short, stable.
            "gh pr view ${PR_NUMBER} --repo ${REPO} --json number,state",
            # The pre-OMN-15407 deploy value: 127 chars but last space at col 84,
            # so a fixpoint. RETAINED as a predicate probe (the producer no longer
            # emits it) because it is the shortest realistic value that is long
            # AND stable — dropping it would leave that combination untested.
            "gh pr diff ${PR_NUMBER} --repo ${REPO} --name-only | "
            "grep -qiE 'nodes/|handlers/|runtime/|services/|docker|monitor_logs|deploy'",
            # OMN-15407: the literal, PR-pinned deploy value the producer emits
            # now. Longer than the placeholder form (a real repo slug is wider
            # than ${REPO}), so it exercises the predicate near its 100-col
            # boundary — where the quoted/block-scalar decision actually flips.
            "gh pr diff 1963 --repo OmniNode-ai/omnimarket --name-only | "
            "grep -ciE 'nodes/|handlers/|runtime/|services/|docker|monitor_logs|deploy'",
            # Same value with the longest real OmniNode repo slug, which pushes
            # the last space past column 100 and must select the literal block.
            "gh pr diff 99999 --repo OmniNode-ai/onex_change_control --name-only | "
            "grep -ciE 'nodes/|handlers/|runtime/|services/|docker|monitor_logs|deploy'",
            # Private-repo receipt-local form: 118 chars, no space past col 100.
            "grep -q '^status: PASS$' $CONTRACT_REPO_DIR/drift/dod_receipts/"
            "OMN-9999/dod-OmniNode-ai-omnimarket-pr-321/command.yaml",
            # The content-bound form — the counter-example to the spec's assumption.
            "gh api repos/OmniNode-ai/omnimarket/contents/src/x.py?ref="
            + "b" * 40
            + " --jq '.content' | base64 -d | grep -c 'class H'",
            # A short-URL synthetic that IS stable, proving the predicate is not
            # a blanket "content reads are always rejected".
            "gh api repos/o/r/contents/x?ref=bbbbbbb --jq '.c'",
        ],
    )
    def test_predicate_agrees_with_the_binary(self, value: str, tmp_path: Path) -> None:
        yamlfmt = shutil.which("yamlfmt")
        if yamlfmt is None:
            pytest.skip("yamlfmt binary not available (installed in the CI gate)")
        assert is_yamlfmt_stable_check(value) is self._real_yamlfmt_is_fixpoint(
            yamlfmt, tmp_path, value
        )


# ---------------------------------------------------------------------------
# Cross-boundary seam parity — driven against the REAL consumer gates
# ---------------------------------------------------------------------------


def _occ_module_names() -> list[str]:
    return [
        name
        for name in sys.modules
        if name == "onex_change_control" or name.startswith("onex_change_control.")
    ]


@contextmanager
def _sibling_occ_package(src: Path) -> Iterator[None]:
    """Make ``<sibling>/src`` authoritative for ``onex_change_control``, then undo it.

    OMN-15539. Inserting ``<sibling>/src`` on ``sys.path`` does nothing once
    ``onex_change_control`` is already in ``sys.modules``: the package's
    ``__path__`` is fixed at first import, so every later submodule resolves
    against whatever won that race. In a full-suite run an earlier test imports
    the INSTALLED ``onex-change-control`` wheel (0.5.1 in ``.venv``), whose
    ``validation/`` package ships only ``patterns`` and ``dogfood_regression`` --
    so the sibling gate script's module-scope
    ``from onex_change_control.validation.evidence_admissibility import ...``
    raises ``ModuleNotFoundError``. The file passes alone and fails in the suite;
    that ordering dependence is the defect, not a flake. The same shadowing is
    also a vacuous-green risk: a wheel that happened to satisfy every import
    would let this "REAL consumer gate" assertion pass against the wheel instead
    of the sibling source it claims to drive.

    The eviction MUST be scoped. Leaving the wheel's modules deleted re-imports
    them later as fresh class objects, and ``isinstance`` against a class taken
    from the pre-eviction import then returns False -- that silently turned
    ``handler_overnight._execute_dispatch_items`` into a no-op and broke seven
    unrelated ``node_overnight`` tests. Restoring the exact prior mapping keeps
    the blast radius inside this loader.
    """
    saved = {name: sys.modules[name] for name in _occ_module_names()}
    for name, module in saved.items():
        origin = getattr(module, "__file__", None)
        if origin is None or not Path(origin).resolve().is_relative_to(src):
            del sys.modules[name]
    try:
        yield
    finally:
        for name in _occ_module_names():
            if name not in saved:
                del sys.modules[name]
        sys.modules.update(saved)


def _load_occ_module(relpath: str, name: str) -> ModuleType | None:
    """Import an onex_change_control gate script from a sibling checkout, or None.

    CLAUDE.md OMN-14208 requires a real cross-boundary regression test driving
    the ACTUAL seam — two independent unit suites would not catch a mismatch
    here. The golden gate workflow clones onex_change_control so this runs for
    real in CI; locally it skips when the sibling checkout is absent.
    """
    root = Path(os.environ.get("OCC_REPO_DIR", "../onex_change_control")).resolve()
    target = root / relpath
    if not target.is_file():
        return None
    src = root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    spec = importlib.util.spec_from_file_location(name, target)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    # Register BEFORE exec: ``dataclasses`` resolves a field's annotation via
    # ``sys.modules[cls.__module__]``, which is None for an unregistered module
    # and raises AttributeError mid-import.
    sys.modules[name] = module
    with _sibling_occ_package(src):
        spec.loader.exec_module(module)
    return module


@pytest.mark.unit
class TestConsumerGateParity:
    """Every generated-byte consumer from the OMN-15247 seams table, for real."""

    @pytest.fixture(scope="class")
    def content_bound_contract(self, tmp_path_factory: pytest.TempPathFactory) -> Path:
        contract = tmp_path_factory.mktemp("content-bound") / "OMN-9999.yaml"
        contract.write_text(
            render_companion_contract(
                ticket_id="OMN-9999",
                repo="OmniNode-ai/omnimarket",
                pr_number=321,
                evidence_id="dod-OmniNode-ai-omnimarket-pr-321",
                downstream_check_value=_CONTENT_BOUND_CHECK,
            )
        )
        return contract

    def test_lint_contract_check_values_accepts_the_generated_contract(
        self, content_bound_contract: Path
    ) -> None:
        module = _load_occ_module(
            "scripts/lint_contract_check_values.py", "occ_lint_check_values"
        )
        if module is None:
            pytest.skip("onex_change_control checkout not available")
        violations = module.lint_contract(content_bound_contract)
        assert violations == [], f"lint-contract-check-values rejects: {violations}"

    def test_substance_floor_derives_proof_tier_l1(
        self, content_bound_contract: Path
    ) -> None:
        module = _load_occ_module(
            "scripts/validation/check_contract_substance_floor.py",
            "occ_substance_floor",
        )
        if module is None:
            pytest.skip("onex_change_control checkout not available")
        downstream = _contract_check_values(content_bound_contract)[0]
        tier = module.derive_proof_tier("command", downstream)
        assert tier.value == "L1", f"expected L1 for {downstream!r}, got {tier}"

    def test_compliance_runner_treats_the_check_as_live_and_hermetic(
        self, content_bound_contract: Path
    ) -> None:
        module = _load_occ_module(
            "src/onex_change_control/scripts/contract_compliance_check.py",
            "occ_compliance_check",
        )
        if module is None:
            pytest.skip("onex_change_control checkout not available")
        downstream = _contract_check_values(content_bound_contract)[0]
        assert module._is_inert_check(downstream) is False
        assert (
            module._non_hermetic_reason(
                {"check_type": "command", "check_value": downstream}
            )
            is None
        )

    def test_the_pinned_ref_survives_token_substitution_unchanged(
        self, content_bound_contract: Path
    ) -> None:
        """There is no ``${SHA}``/``${MERGE_BASE}`` token — the ref must be literal."""
        module = _load_occ_module(
            "src/onex_change_control/scripts/contract_compliance_check.py",
            "occ_compliance_check",
        )
        if module is None:
            pytest.skip("onex_change_control checkout not available")
        downstream = _contract_check_values(content_bound_contract)[0]
        substituted = module._substitute_tokens(
            downstream,
            pr_number=321,
            repo="OmniNode-ai/omnimarket",
            ticket_id="OMN-9999",
        )
        assert f"?ref={_HEAD_SHA}" in substituted
        # No token vocabulary touched the literal ref; the command is unchanged.
        assert substituted == downstream
