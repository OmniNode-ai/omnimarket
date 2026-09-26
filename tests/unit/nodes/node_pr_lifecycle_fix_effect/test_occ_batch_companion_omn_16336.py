# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ticket-scoped OCC batch companion pilot (OMN-16336)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import unquote
from uuid import uuid4

import pytest
import yaml
from click.testing import CliRunner
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.events.occ_companion import (
    EnumOccBatchMode,
    batch_companion_branch_for,
    companion_branch_for,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_batch_companion import (
    batch_member_bases,
    extract_dod_evidence_blocks,
    member_evidence_ids,
    remove_dod_evidence_items,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
    OccCompanionEmitter,
    StaleBatchHeadError,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    ADMISSIBILITY_VALIDATOR_EVIDENCE_ID,
    BEHAVIOR_PROOF_EVIDENCE_ID,
    append_dod_evidence_items,
    ci_check_evidence_id,
    pr_scoped_slot_evidence_id,
    render_companion_contract,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    ModelPrLifecycleFixCommand,
)

_ROOT = Path(__file__).resolve().parents[4]
_PUBLISHER = _ROOT / "scripts" / "publish_occ_autobind_command.py"
_REPO = "OmniNode-ai/omnimarket"
_TICKET = "OMN-16336"


def _load_publisher() -> object:
    spec = importlib.util.spec_from_file_location("occ_batch_publisher", _PUBLISHER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
@pytest.mark.parametrize("dev_has_contract", [False, True])
def test_batch_helper_round_trip_preserves_dev_contract(
    dev_has_contract: bool,
) -> None:
    contract = render_companion_contract(
        ticket_id=_TICKET,
        repo=_REPO,
        pr_number=101,
        evidence_id="dod-OmniNode-ai-omnimarket-pr-101",
    )
    ids_a = member_evidence_ids(repo=_REPO, pr_number=101)
    blocks = extract_dod_evidence_blocks(contract, ids_a)
    base = remove_dod_evidence_items(contract, ids_a)
    if dev_has_contract:
        base = base.replace(
            "dod_evidence:\n",
            "dod_evidence:\n"
            "  - id: dod-unrelated-pr-1\n"
            "    description: unrelated dev evidence\n"
            "    checks: []\n",
        )
    rebuilt = append_dod_evidence_items(base, blocks)
    parsed = yaml.safe_load(rebuilt)
    parsed_original = yaml.safe_load(contract)
    original_ids = {item["id"] for item in parsed_original["dod_evidence"]}
    assert {item["id"] for item in parsed["dod_evidence"]} >= ids_a & original_ids
    if not dev_has_contract:
        assert parsed == parsed_original


@pytest.mark.unit
def test_batch_mode_off_is_byte_identical() -> None:
    module = _load_publisher()
    payload = module.build_payload(_REPO, 42, _TICKET, str(uuid4()))  # type: ignore[attr-defined]
    assert "occ_batch_mode" not in payload
    command = ModelPrLifecycleFixCommand.model_validate(json.loads(json.dumps(payload)))
    assert command.occ_batch_mode is EnumOccBatchMode.OFF


@pytest.mark.unit
def test_batch_rebuild_retries_remote_head_movement_at_most_three_times() -> None:
    emitter = OccCompanionEmitter()
    with patch.object(
        emitter,
        "_emit_companion_sync_once",
        side_effect=[
            StaleBatchHeadError("moved once"),
            StaleBatchHeadError("moved twice"),
            "rebuilt",
        ],
    ) as attempt:
        result = emitter._emit_companion_sync(
            _REPO, 42, _TICKET, batch_mode=EnumOccBatchMode.TICKET
        )
    assert result == "rebuilt"
    assert attempt.call_count == 3


@pytest.mark.unit
def test_publisher_emits_batch_mode_only_when_ticket() -> None:
    module = _load_publisher()
    off = module.build_payload(  # type: ignore[attr-defined]
        _REPO, 42, _TICKET, str(uuid4()), batch_mode=EnumOccBatchMode.OFF
    )
    ticket = module.build_payload(  # type: ignore[attr-defined]
        _REPO, 42, _TICKET, str(uuid4()), batch_mode=EnumOccBatchMode.TICKET
    )
    assert "occ_batch_mode" not in off
    assert ticket["occ_batch_mode"] == "ticket"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("arguments", "extra_env"),
    [(["--batch-mode", "ticket"], {}), ([], {"OCC_COMPANION_BATCH_MODE": "ticket"})],
)
def test_publisher_cli_enables_ticket_batch(
    arguments: list[str], extra_env: dict[str, str]
) -> None:
    module = _load_publisher()
    result = CliRunner().invoke(
        module.main,  # type: ignore[attr-defined]
        ["--dry-run", *arguments],
        env={
            "PR_REPO": _REPO,
            "PR_NUMBER": "42",
            "PR_HEAD_SHA": "a" * 40,
            "PR_TITLE": f"feat({_TICKET}): batch",
            **extra_env,
        },
    )
    assert result.exit_code == 0, result.output
    assert '"occ_batch_mode": "ticket"' in result.output


@pytest.mark.unit
def test_workflows_enable_only_the_ticket_batch_pilot() -> None:
    autobind = (_ROOT / ".github/workflows/call-occ-autobind.yml").read_text()
    runner = (_ROOT / ".github/workflows/occ-receipt-runner.yml").read_text()
    assert "BATCH PILOT (OMN-16336)" in autobind
    assert "vars.OMNI_OCC_COMPANION_BATCH_MODE || 'off'" in autobind
    assert "closed" in yaml.safe_load(autobind)[True]["pull_request"]["types"]
    assert "auto/ticket-" in runner
    assert "attempt" in runner
    assert "pull --rebase" in runner


@pytest.mark.unit
def test_member_helpers_use_exact_evidence_id_shapes() -> None:
    base = "dod-OmniNode-ai-omnimarket-pr-42"
    assert member_evidence_ids(repo=_REPO, pr_number=42) == frozenset(
        {
            base,
            ci_check_evidence_id(base),
            pr_scoped_slot_evidence_id(
                BEHAVIOR_PROOF_EVIDENCE_ID, repo=_REPO, pr_number=42
            ),
            pr_scoped_slot_evidence_id(
                ADMISSIBILITY_VALIDATOR_EVIDENCE_ID, repo=_REPO, pr_number=42
            ),
        }
    )
    paths = [
        f"drift/dod_receipts/{_TICKET}/{base}/command.yaml",
        f"drift/dod_receipts/{_TICKET}/{base}-ci/command.yaml",
        f"drift/dod_receipts/{_TICKET}/{base}/command.supersede.42.yaml",
        "README.md",
    ]
    assert batch_member_bases(_TICKET, paths) == (base,)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    ).stdout.strip()


def _git_result(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    )


class _BatchScenario:
    """Bare OCC origin plus stateful product/OCC REST surfaces."""

    def __init__(self, root: Path, *, dev_variant: str = "unchanged") -> None:
        self.root = root
        self.origin = root / "occ-origin.git"
        self.seed = root / "seed"
        self.origin.mkdir(parents=True)
        self.seed.mkdir()
        _git(self.origin, "init", "--bare")
        _git(self.seed, "init")
        _git(self.seed, "config", "user.name", "test")
        _git(self.seed, "config", "user.email", "test@example.com")
        (self.seed / "README.md").write_text("OCC fixture\n", encoding="utf-8")
        if dev_variant == "earlier_ticket_entry":
            contract_path = self.seed / "contracts" / f"{_TICKET}.yaml"
            contract_path.parent.mkdir(parents=True)
            contract_path.write_text(
                render_companion_contract(
                    ticket_id=_TICKET,
                    repo="OmniNode-ai/omnibase_core",
                    pr_number=99,
                    evidence_id="dod-OmniNode-ai-omnibase_core-pr-99",
                ),
                encoding="utf-8",
            )
        _git(self.seed, "add", "-A")
        _git(self.seed, "commit", "-m", "seed OCC dev")
        _git(self.seed, "branch", "-M", "dev")
        _git(self.seed, "remote", "add", "origin", str(self.origin))
        _git(self.seed, "push", "-u", "origin", "dev")
        _git(self.origin, "symbolic-ref", "HEAD", "refs/heads/dev")

        self.bodies = {101: "A", 102: "B"}
        self.states = {101: "open", 102: "open"}
        self.merged = {101: False, 102: False}
        self.occ_prs: dict[int, dict[str, Any]] = {}
        self.branch_prs: dict[str, int] = {}
        self.pull_posts: list[dict[str, Any]] = []
        self._next_occ_pr = 55
        self._clock_ticks = 0

    def product(self, pr_number: int) -> dict[str, object]:
        return {
            "number": pr_number,
            "body": self.bodies[pr_number],
            "title": f"feat({_TICKET}): member {pr_number}",
            "head": {"sha": str(pr_number)[:1] * 40, "ref": f"member-{pr_number}"},
            "base": {"repo": {"private": False}},
            "state": self.states[pr_number],
            "merged": self.merged[pr_number],
            "draft": False,
            "labels": [],
        }

    def fake_rest(
        self,
        method: str,
        path: str,
        *,
        body: object = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        del token
        for number in (101, 102):
            if path == f"/repos/OmniNode-ai/omnimarket/pulls/{number}":
                if method == "PATCH" and isinstance(body, dict):
                    self.bodies[number] = str(body.get("body") or self.bodies[number])
                return self.product(number)

        if path.startswith("/search/issues"):
            decoded = unquote(path)
            branch = decoded.split("head:", maxsplit=1)[1]
            existing_number = self.branch_prs.get(branch)
            if (
                existing_number is None
                or self.occ_prs[existing_number]["state"] != "open"
            ):
                return {"items": []}
            return {"items": [{"number": existing_number}]}
        if path == "/repos/OmniNode-ai/onex_change_control":
            return {"default_branch": "dev"}
        if path == "/repos/OmniNode-ai/onex_change_control/pulls" and method == "POST":
            assert isinstance(body, dict)
            posted_branch = body.get("head")
            assert isinstance(posted_branch, str)
            number = self._next_occ_pr
            self._next_occ_pr += 1
            self.branch_prs[posted_branch] = number
            self.occ_prs[number] = {
                "number": number,
                "state": "open",
                "merged": False,
                "merged_at": None,
                "mergeable": True,
                "mergeable_state": "clean",
                "head": {"ref": posted_branch},
                "body": body.get("body"),
            }
            self.pull_posts.append(dict(body))
            return {"number": number}

        pull_match = path.removeprefix("/repos/OmniNode-ai/onex_change_control/pulls/")
        if pull_match.isdigit():
            number = int(pull_match)
            pull = self.occ_prs[number]
            if method == "PATCH" and isinstance(body, dict):
                pull.update(body)
            return dict(pull)
        if path.startswith("/repos/OmniNode-ai/onex_change_control/issues/"):
            return {}
        return {}

    def fake_array(
        self,
        method: str,
        path: str,
        *,
        body: object = None,
        token: str | None = None,
    ) -> list[dict[str, object]]:
        del method, body, token
        match = path.split("/pulls/", maxsplit=1)
        if len(match) != 2 or "/files" not in match[1]:
            return []
        number_text = match[1].split("/", maxsplit=1)[0]
        if not number_text.isdigit():
            return []
        pull = self.occ_prs[int(number_text)]
        head = pull.get("head")
        assert isinstance(head, dict)
        branch = head.get("ref")
        assert isinstance(branch, str)
        names = _git(
            self.origin,
            "diff",
            "--name-only",
            f"refs/heads/dev..refs/heads/{branch}",
        ).splitlines()
        return [{"filename": name} for name in names]

    def patches(self, emitter: OccCompanionEmitter) -> ExitStack:
        module = (
            "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers."
            "occ_companion_emitter"
        )
        scenario = self

        class _ScenarioDateTime:
            @staticmethod
            def now(tz: object = None) -> datetime:
                scenario._clock_ticks += 1
                value = datetime(2026, 9, 26, tzinfo=UTC) + timedelta(
                    seconds=scenario._clock_ticks
                )
                return value if tz is not None else value.replace(tzinfo=None)

        stack = ExitStack()
        stack.enter_context(patch(f"{module}.rest_json", side_effect=self.fake_rest))
        stack.enter_context(
            patch(f"{module}.rest_json_array", side_effect=self.fake_array)
        )
        stack.enter_context(
            patch(f"{module}._resolve_github_token", return_value="token")
        )
        stack.enter_context(
            patch(f"{module}._resolve_product_token", return_value=("token", True))
        )
        stack.enter_context(
            patch(f"{module}.authenticated_occ_url", return_value=str(self.origin))
        )
        stack.enter_context(
            patch(f"{module}.acquire_occ_ticket_lease", return_value=True)
        )
        stack.enter_context(patch(f"{module}.release_occ_ticket_lease"))
        stack.enter_context(
            patch(f"{module}.acquire_occ_companion_lease", return_value=True)
        )
        stack.enter_context(patch(f"{module}.release_occ_companion_lease"))
        stack.enter_context(patch(f"{module}.read_ticket_ac_bindings", return_value=()))
        stack.enter_context(patch(f"{module}.datetime", _ScenarioDateTime))
        stack.enter_context(
            patch.object(emitter, "_find_contending_companions", return_value=[])
        )
        stack.enter_context(
            patch.object(
                emitter,
                "_derive_content_bound_check",
                return_value=(
                    "gh api repos/OmniNode-ai/omnimarket/contents/src/x.py?ref="
                    + "1" * 40,
                    "a" * 40,
                    1,
                ),
            )
        )
        stack.enter_context(
            patch.object(
                emitter,
                "_observe_pr_probe",
                return_value=('{"files":[{"path":"src/x.py"}]}', 0),
            )
        )
        return stack

    def emit(
        self,
        emitter: OccCompanionEmitter,
        pr_number: int,
        *,
        batch_mode: EnumOccBatchMode = EnumOccBatchMode.TICKET,
    ) -> str:
        return emitter._emit_companion_sync(
            _REPO, pr_number, _TICKET, batch_mode=batch_mode
        )

    def advance_dev_unrelated(self) -> None:
        unrelated = self.seed / "contracts" / "OMN-99999.yaml"
        unrelated.parent.mkdir(parents=True, exist_ok=True)
        unrelated.write_text("ticket_id: OMN-99999\n", encoding="utf-8")
        _git(self.seed, "add", str(unrelated.relative_to(self.seed)))
        _git(self.seed, "commit", "-m", "advance unrelated OCC ticket")
        _git(self.seed, "push", "origin", "dev")

    def branch_head(self, branch: str) -> str:
        return _git(self.origin, "rev-parse", f"refs/heads/{branch}")

    def branch_paths(self, branch: str) -> list[str]:
        return _git(
            self.origin, "ls-tree", "-r", "--name-only", f"refs/heads/{branch}"
        ).splitlines()

    def show(self, branch: str, path: str) -> str:
        return self.show_bytes(branch, path).decode()

    def show_bytes(self, branch: str, path: str) -> bytes:
        return subprocess.run(
            ["git", "show", f"refs/heads/{branch}:{path}"],
            cwd=self.origin,
            check=True,
            capture_output=True,
            env=scrub_git_location_env(os.environ),
        ).stdout

    def member_receipts(self, branch: str, pr_number: int) -> dict[str, bytes]:
        ids = member_evidence_ids(repo=_REPO, pr_number=pr_number)
        paths = {
            path
            for path in self.branch_paths(branch)
            if len(path.split("/")) >= 5 and path.split("/")[3] in ids
        }
        return {
            path: subprocess.run(
                ["git", "show", f"refs/heads/{branch}:{path}"],
                cwd=self.origin,
                check=True,
                capture_output=True,
                env=scrub_git_location_env(os.environ),
            ).stdout
            for path in sorted(paths)
        }


def _contract_ids(contract_text: str) -> list[str]:
    contract = yaml.safe_load(contract_text)
    return [item["id"] for item in contract["dod_evidence"]]


def _without_contract_hash(receipt_bytes: bytes) -> dict[str, Any]:
    receipt = dict(yaml.safe_load(receipt_bytes))
    receipt.pop("contract_sha256", None)
    return receipt


def _scenario_member_ids(pr_number: int) -> set[str]:
    """Ids rendered by this fixture's no-behavior-test-path diff."""
    ids = set(member_evidence_ids(repo=_REPO, pr_number=pr_number))
    ids.remove(
        pr_scoped_slot_evidence_id(
            BEHAVIOR_PROOF_EVIDENCE_ID, repo=_REPO, pr_number=pr_number
        )
    )
    return ids


@pytest.mark.unit
def test_code_pr_head_binds_to_its_batch_entry(tmp_path: Path) -> None:
    scenario = _BatchScenario(tmp_path)
    emitter = OccCompanionEmitter()
    with scenario.patches(emitter):
        scenario.emit(emitter, 101)
        scenario.emit(emitter, 102)

        assert emitter._occ_binding_matches_this_pr(
            occ_pr_number=55, repo=_REPO, pr_number=101, token="token"
        )
        assert emitter._occ_binding_matches_this_pr(
            occ_pr_number=55, repo=_REPO, pr_number=102, token="token"
        )
        assert not emitter._occ_binding_matches_this_pr(
            occ_pr_number=55, repo=_REPO, pr_number=103, token="token"
        )

    assert "Evidence-Source: OCC#55" in scenario.bodies[101]
    assert "Evidence-Source: OCC#55" in scenario.bodies[102]


@pytest.mark.unit
def test_update_branch_remint_updates_entry_not_new_pr(tmp_path: Path) -> None:
    scenario = _BatchScenario(tmp_path)
    emitter = OccCompanionEmitter()
    branch = batch_companion_branch_for(_TICKET)
    with scenario.patches(emitter):
        scenario.emit(emitter, 101)
        scenario.emit(emitter, 102)
        before_a = scenario.member_receipts(branch, 101)
        before_b = scenario.member_receipts(branch, 102)
        before_noop_head = scenario.branch_head(branch)

        action = scenario.emit(emitter, 102)

        assert action.startswith("no-op:")
        assert scenario.branch_head(branch) == before_noop_head
        assert len(scenario.pull_posts) == 1

        scenario.occ_prs[55]["mergeable"] = False
        scenario.occ_prs[55]["mergeable_state"] = "dirty"
        scenario.emit(emitter, 102)

    after_a = scenario.member_receipts(branch, 101)
    after_b = scenario.member_receipts(branch, 102)
    assert scenario.branch_prs[branch] == 55
    assert len(scenario.pull_posts) == 1
    assert scenario.branch_head(branch) != before_noop_head
    assert _git(
        scenario.origin, "for-each-ref", "--format=%(refname)", "refs/heads/auto"
    ).splitlines() == [f"refs/heads/{branch}"]
    reminted_ids = _contract_ids(scenario.show(branch, f"contracts/{_TICKET}.yaml"))
    for item_id in _scenario_member_ids(102):
        assert reminted_ids.count(item_id) == 1
    assert before_b.keys() == after_b.keys()
    assert any(before_b[path] != after_b[path] for path in before_b)
    assert before_a.keys() == after_a.keys()
    for path in before_a:
        assert _without_contract_hash(before_a[path]) == _without_contract_hash(
            after_a[path]
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "dev_variant", ["unchanged", "unrelated_advance", "earlier_ticket_entry"]
)
def test_two_code_prs_one_ticket_do_not_conflict(
    tmp_path: Path, dev_variant: str
) -> None:
    scenario = _BatchScenario(tmp_path / "batch", dev_variant=dev_variant)
    emitter = OccCompanionEmitter()
    with scenario.patches(emitter):
        scenario.emit(emitter, 101)
        if dev_variant == "unrelated_advance":
            scenario.advance_dev_unrelated()
        scenario.emit(emitter, 102)

    branch = batch_companion_branch_for(_TICKET)
    assert len(scenario.pull_posts) == 1
    assert _git(
        scenario.origin, "for-each-ref", "--format=%(refname)", "refs/heads/auto"
    ).splitlines() == [f"refs/heads/{branch}"]
    final_contract_text = scenario.show(branch, f"contracts/{_TICKET}.yaml")
    final_ids = _contract_ids(final_contract_text)
    dev_contract = _git_result(
        scenario.origin, "show", f"refs/heads/dev:contracts/{_TICKET}.yaml"
    )
    dev_ids = _contract_ids(dev_contract.stdout) if dev_contract.returncode == 0 else []
    member_ids = _scenario_member_ids(101) | _scenario_member_ids(102)
    expected_ids = set(dev_ids) | member_ids | {"occ-self-bind-pr-55"}
    assert set(final_ids) == expected_ids
    assert all(final_ids.count(item_id) == 1 for item_id in expected_ids)

    checkout = tmp_path / "batch-merge-check"
    _git(tmp_path, "clone", str(scenario.origin), str(checkout))
    _git(checkout, "config", "user.name", "test")
    _git(checkout, "config", "user.email", "test@example.com")
    _git(checkout, "merge", "--no-edit", f"origin/{branch}")
    final_contract_path = checkout / "contracts" / f"{_TICKET}.yaml"
    digest = hashlib.sha256(final_contract_path.read_bytes()).hexdigest()
    receipts = list((checkout / "drift" / "dod_receipts" / _TICKET).rglob("*.yaml"))
    assert receipts
    for receipt_path in receipts:
        receipt = yaml.safe_load(receipt_path.read_text(encoding="utf-8"))
        assert receipt["contract_sha256"] == f"sha256:{digest}"

    control = _BatchScenario(tmp_path / "control", dev_variant=dev_variant)
    control_emitter = OccCompanionEmitter()
    with control.patches(control_emitter):
        control.emit(control_emitter, 101, batch_mode=EnumOccBatchMode.OFF)
        control.emit(control_emitter, 102, batch_mode=EnumOccBatchMode.OFF)
    branch_a = companion_branch_for(_REPO, 101)
    branch_b = companion_branch_for(_REPO, 102)
    control_checkout = tmp_path / "control-merge-check"
    _git(tmp_path, "clone", str(control.origin), str(control_checkout))
    _git(control_checkout, "config", "user.name", "test")
    _git(control_checkout, "config", "user.email", "test@example.com")
    _git(control_checkout, "merge", "--no-edit", f"origin/{branch_a}")
    second_merge = _git_result(
        control_checkout, "merge", "--no-edit", f"origin/{branch_b}"
    )
    assert second_merge.returncode != 0


@pytest.mark.unit
def test_abandoned_code_pr_entry_removed(tmp_path: Path) -> None:
    scenario = _BatchScenario(tmp_path)
    emitter = OccCompanionEmitter()
    branch = batch_companion_branch_for(_TICKET)
    ids_a = member_evidence_ids(repo=_REPO, pr_number=101)
    rendered_ids_b = _scenario_member_ids(102)

    with scenario.patches(emitter):
        scenario.emit(emitter, 101)
        scenario.emit(emitter, 102)
        before_b = scenario.member_receipts(branch, 102)

        scenario.states[101] = "closed"
        removed = scenario.emit(emitter, 101)

        assert removed.startswith("removed ")
        contract_text = scenario.show(branch, f"contracts/{_TICKET}.yaml")
        contract_ids = _contract_ids(contract_text)
        final_paths = scenario.branch_paths(branch)
        assert ids_a.isdisjoint(contract_ids)
        assert rendered_ids_b.issubset(contract_ids)
        assert not any(
            len(path.split("/")) >= 5 and path.split("/")[3] in ids_a
            for path in final_paths
        )
        assert all(
            any(
                len(path.split("/")) >= 5 and path.split("/")[3] == item_id
                for path in final_paths
            )
            for item_id in rendered_ids_b
        )
        after_b = scenario.member_receipts(branch, 102)
        digest = hashlib.sha256(
            scenario.show_bytes(branch, f"contracts/{_TICKET}.yaml")
        ).hexdigest()
        assert before_b.keys() == after_b.keys()
        for path in before_b:
            assert _without_contract_hash(before_b[path]) == _without_contract_hash(
                after_b[path]
            )
            assert (
                yaml.safe_load(after_b[path])["contract_sha256"] == f"sha256:{digest}"
            )

        scenario.states[102] = "closed"
        scenario.merged[102] = True
        merged_head = scenario.branch_head(branch)
        merged_action = scenario.emit(emitter, 102)
        assert "immutable" in merged_action
        assert scenario.branch_head(branch) == merged_head

        scenario.merged[102] = False
        sole_member_head = scenario.branch_head(branch)
        close_action = scenario.emit(emitter, 102)
        assert close_action.startswith("closed empty OCC batch")
        assert scenario.occ_prs[55]["state"] == "closed"
        assert scenario.branch_head(branch) == sole_member_head
