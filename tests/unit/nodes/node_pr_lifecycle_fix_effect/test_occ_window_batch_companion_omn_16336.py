# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cross-ticket OCC batch window: one companion per product repo (OMN-16336).

The operator ruling of 2026-09-27T10:32:50Z asked for one change-control
companion for a batch of PRs, not one per PR and not one per ticket. The window
mode keys the companion on the product repository: every open member PR shares
one branch and one OCC pull request, and each member's evidence lives in its
own ticket's contract and receipt folder, exactly where the per-PR path writes
it. These tests drive the real emitter against a bare git origin and prove, for
PRs citing DIFFERENT tickets, that one companion carries both, that a member
change rebuilds it in place, that a closed member is dropped, and that the
receipt gate's own eligibility validator still passes for every member.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
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
from omnibase_core.models.validation.model_occ_eligibility_input import (
    ModelOccEligibilityInput,
)
from omnibase_core.validation.validator_occ_merge_eligibility import (
    validate_occ_merge_eligibility,
)
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.events.occ_companion import (
    EnumOccBatchMode,
    batch_companion_branch_for,
    companion_branch_for,
    is_batch_companion_branch,
    repo_slug_of_window_branch,
    window_companion_branch_for,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix import (
    _NoopOccAutobindAdapter,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_batch_companion import (
    batch_tickets,
    member_evidence_ids,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
    OccCompanionEmitter,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    BEHAVIOR_PROOF_EVIDENCE_ID,
    pr_scoped_slot_evidence_id,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    ModelPrLifecycleFixCommand,
)

_ROOT = Path(__file__).resolve().parents[4]
_REPO = "OmniNode-ai/omnimarket"
_OCC = "OmniNode-ai/onex_change_control"
_TICKETS = {101: "OMN-11111", 102: "OMN-22222", 103: "OMN-11111"}
_HEADS = {101: "a" * 40, 102: "b" * 40, 103: "c" * 40}
_WINDOW = window_companion_branch_for(_REPO)


def _load_script(name: str) -> Any:
    path = _ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(path.stem + "_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    ).stdout.strip()


class _WindowScenario:
    """Bare OCC origin plus stateful product/OCC REST surfaces, many tickets."""

    def __init__(self, root: Path, members: tuple[int, ...] = (101, 102)) -> None:
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
        _git(self.seed, "add", "-A")
        _git(self.seed, "commit", "-m", "seed OCC dev")
        _git(self.seed, "branch", "-M", "dev")
        _git(self.seed, "remote", "add", "origin", str(self.origin))
        _git(self.seed, "push", "-u", "origin", "dev")
        _git(self.origin, "symbolic-ref", "HEAD", "refs/heads/dev")

        self.bodies = {number: f"member {number}" for number in members}
        self.states = dict.fromkeys(members, "open")
        self.merged = dict.fromkeys(members, False)
        self.occ_prs: dict[int, dict[str, Any]] = {}
        self.branch_prs: dict[str, int] = {}
        self.pull_posts: list[dict[str, Any]] = []
        self._next_occ_pr = 55
        self._clock_ticks = 0

    def title(self, pr_number: int) -> str:
        return f"feat({_TICKETS[pr_number]}): member {pr_number}"

    def product(self, pr_number: int) -> dict[str, object]:
        return {
            "number": pr_number,
            "body": self.bodies[pr_number],
            "title": self.title(pr_number),
            "head": {"sha": _HEADS[pr_number], "ref": f"member-{pr_number}"},
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
        for number in self.bodies:
            if path == f"/repos/{_REPO}/pulls/{number}":
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
        if path == f"/repos/{_OCC}":
            return {"default_branch": "dev"}
        if path == f"/repos/{_OCC}/pulls" and method == "POST":
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
                "title": body.get("title"),
                "body": body.get("body"),
            }
            self.pull_posts.append(dict(body))
            return {"number": number}

        pull_match = path.removeprefix(f"/repos/{_OCC}/pulls/")
        if pull_match.isdigit():
            number = int(pull_match)
            pull = self.occ_prs[number]
            if method == "PATCH" and isinstance(body, dict):
                pull.update(body)
            return dict(pull)
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
        branch = pull["head"]["ref"]
        names = _git(
            self.origin,
            "diff",
            "--name-only",
            f"refs/heads/dev..refs/heads/{branch}",
        ).splitlines()
        return [{"filename": name} for name in names]

    def patches(self, emitter: OccCompanionEmitter) -> tuple[ExitStack, dict[str, Any]]:
        module = (
            "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers."
            "occ_companion_emitter"
        )
        scenario = self

        class _ScenarioDateTime:
            @staticmethod
            def now(tz: object = None) -> datetime:
                scenario._clock_ticks += 1
                value = datetime(2026, 9, 27, tzinfo=UTC) + timedelta(
                    seconds=scenario._clock_ticks
                )
                return value if tz is not None else value.replace(tzinfo=None)

        stack = ExitStack()
        mocks: dict[str, Any] = {}
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
        mocks["acquire_window"] = stack.enter_context(
            patch(f"{module}.acquire_occ_window_lease", return_value=True)
        )
        mocks["release_window"] = stack.enter_context(
            patch(f"{module}.release_occ_window_lease")
        )
        mocks["acquire_ticket"] = stack.enter_context(
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
                side_effect=lambda **kwargs: (
                    f"gh api repos/{_REPO}/contents/src/x.py?ref="
                    + str(kwargs["evidence_ref"]),
                    "d" * 40,
                    1,
                    (),
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
        return stack, mocks

    def emit(
        self,
        emitter: OccCompanionEmitter,
        pr_number: int,
        *,
        batch_mode: EnumOccBatchMode = EnumOccBatchMode.WINDOW,
    ) -> str:
        return emitter._emit_companion_sync(
            _REPO, pr_number, _TICKETS[pr_number], batch_mode=batch_mode
        )

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

    def member_receipts(self, branch: str, pr_number: int) -> dict[str, str]:
        ids = member_evidence_ids(repo=_REPO, pr_number=pr_number)
        return {
            path: self.show(branch, path)
            for path in sorted(self.branch_paths(branch))
            if len(path.split("/")) >= 5 and path.split("/")[3] in ids
        }

    def checkout(self, branch: str, into: Path) -> Path:
        _git(self.root, "clone", "--quiet", str(self.origin), str(into))
        _git(into, "checkout", "--quiet", branch)
        return into


def _rendered_member_ids(pr_number: int) -> set[str]:
    """Ids this fixture's diff renders (no behaviour test path is derivable)."""
    ids = set(member_evidence_ids(repo=_REPO, pr_number=pr_number))
    ids.remove(
        pr_scoped_slot_evidence_id(
            BEHAVIOR_PROOF_EVIDENCE_ID, repo=_REPO, pr_number=pr_number
        )
    )
    return ids


def _contract_ids(contract_text: str) -> list[str]:
    return [item["id"] for item in yaml.safe_load(contract_text)["dod_evidence"]]


def _without_contract_hashes(text: str) -> dict[str, Any]:
    receipt = dict(yaml.safe_load(text))
    receipt.pop("contract_sha256", None)
    receipt.pop("contract_entry_sha256", None)
    return receipt


def _eligibility(scenario: _WindowScenario, checkout: Path, pr_number: int) -> Any:
    return validate_occ_merge_eligibility(
        ModelOccEligibilityInput(
            repo=_REPO,
            pr_number=pr_number,
            pr_title=scenario.title(pr_number),
            pr_body=scenario.bodies[pr_number],
            pr_branch=f"member-{pr_number}",
            pr_commit_shas=(_HEADS[pr_number],),
            pr_commit_texts=(),
            occ_commit_sha=_git(checkout, "rev-parse", "HEAD"),
            contracts_dir=checkout / "contracts",
            receipts_dir=checkout / "drift" / "dod_receipts",
        )
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_window_branch_is_keyed_on_the_product_repo() -> None:
    branch = window_companion_branch_for(_REPO)
    assert branch == "auto/window-omninode-ai-omnimarket-occ-autobind"
    assert repo_slug_of_window_branch(branch) == "omninode-ai-omnimarket"
    assert is_batch_companion_branch(branch)
    assert is_batch_companion_branch(batch_companion_branch_for("OMN-1"))
    assert not is_batch_companion_branch(companion_branch_for(_REPO, 7))
    assert repo_slug_of_window_branch(companion_branch_for(_REPO, 7)) is None


@pytest.mark.unit
def test_batch_tickets_lists_every_member_ticket_once() -> None:
    paths = [
        "drift/dod_receipts/OMN-11111/dod-OmniNode-ai-omnimarket-pr-101/command.yaml",
        "drift/dod_receipts/OMN-22222/dod-OmniNode-ai-omnimarket-pr-102/command.yaml",
        "drift/dod_receipts/OMN-11111/dod-OmniNode-ai-omnimarket-pr-103-ci/command.yaml",
        "drift/dod_receipts/OMN-33333/occ-self-bind-pr-55/command.yaml",
        "contracts/OMN-11111.yaml",
    ]
    assert batch_tickets(paths) == ("OMN-11111", "OMN-22222")


# ---------------------------------------------------------------------------
# Window is the default, and the flag may only turn batching off
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_window_is_the_default_grouping_everywhere() -> None:
    command = ModelPrLifecycleFixCommand.model_validate(
        {
            "correlation_id": str(uuid4()),
            "pr_number": 42,
            "repo": _REPO,
            "block_reason": "receipt_evidence_source_autobind",
            "requested_at": "2026-09-27T12:00:00+00:00",
        }
    )
    assert command.occ_batch_mode is EnumOccBatchMode.WINDOW
    emitter_default = OccCompanionEmitter.autobind_evidence_source.__kwdefaults__
    assert emitter_default is not None
    assert emitter_default["batch_mode"] is EnumOccBatchMode.WINDOW
    noop_default = _NoopOccAutobindAdapter.autobind_evidence_source.__kwdefaults__
    assert noop_default is not None
    assert noop_default["batch_mode"] is EnumOccBatchMode.WINDOW


@pytest.mark.unit
@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        (None, None),
        ("", None),
        ("window", None),
        ("ticket", None),
        ("WINDOW", None),
        ("off", "off"),
        ("OFF", "off"),
    ],
)
def test_publisher_flag_only_turns_batching_off(
    env_value: str | None, expected: str | None
) -> None:
    """Unset, empty or any batching value keeps the runtime default (window).

    The field is left off the wire unless batching is turned off, so a runtime
    that predates window mode still validates the command.
    """
    module = _load_script("publish_occ_autobind_command.py")
    env = {
        "PR_REPO": _REPO,
        "PR_NUMBER": "42",
        "PR_HEAD_SHA": "a" * 40,
        "PR_TITLE": "feat(OMN-11111): x",
    }
    if env_value is not None:
        env["OCC_COMPANION_BATCH_MODE"] = env_value
    result = CliRunner().invoke(module.main, ["--dry-run"], env=env)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload.get("occ_batch_mode") == expected


@pytest.mark.unit
@pytest.mark.parametrize(("mode", "wire"), [("window", None), ("off", "off")])
def test_publisher_build_payload_wire_shape(mode: str, wire: str | None) -> None:
    module = _load_script("publish_occ_autobind_command.py")
    payload = module.build_payload(_REPO, 42, "OMN-11111", str(uuid4()), mode)
    assert payload.get("occ_batch_mode") == wire
    command = ModelPrLifecycleFixCommand.model_validate(json.loads(json.dumps(payload)))
    assert command.occ_batch_mode is EnumOccBatchMode(mode)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("env_value", "declines"),
    [(None, True), ("", True), ("window", True), ("off", False)],
)
def test_companion_effect_publisher_defers_to_the_batch_window(
    env_value: str | None, declines: bool
) -> None:
    """With batching on, the per-PR companion effect leg must not mint.

    Otherwise it races the autobind window and leaves a second, per-PR
    companion for the same product PR. It declines loudly, with the
    ``publish_declined:`` verdict marker the calling workflow requires.
    """
    module = _load_script("publish_occ_companion_effect_command.py")
    env = {
        "PR_REPO": _REPO,
        "PR_NUMBER": "42",
        "PR_HEAD_SHA": "a" * 40,
        "PR_BODY": "",
    }
    if env_value is not None:
        env["OCC_COMPANION_BATCH_MODE"] = env_value
    result = CliRunner().invoke(module.main, ["--dry-run"], env=env)
    assert result.exit_code == 0, result.output
    assert (
        "publish_declined: occ_companion_batch_window_owns_this_pr" in result.output
    ) is declines
    if not declines:
        assert "publish_declined: dry_run" in result.output


@pytest.mark.unit
def test_omnimarket_workflow_passes_the_flag_without_an_off_default() -> None:
    text = (_ROOT / ".github/workflows/call-occ-autobind.yml").read_text()
    assert "vars.OMNI_OCC_COMPANION_BATCH_MODE || 'off'" not in text
    assert "OCC_COMPANION_BATCH_MODE: ${{ vars.OMNI_OCC_COMPANION_BATCH_MODE }}" in text
    workflow = yaml.safe_load(text)
    assert "closed" in workflow[True]["pull_request"]["types"]
    condition = workflow["jobs"]["publish-occ-autobind"]["if"]
    # The closed-unmerged drop runs whatever the flag says; the emitter drops
    # a closed member from its window, or skips it when batching is off.
    assert "vars." not in condition
    assert "!github.event.pull_request.merged" in condition


@pytest.mark.unit
def test_receipt_runner_accepts_the_repo_window_branch() -> None:
    runner = (_ROOT / ".github/workflows/occ-receipt-runner.yml").read_text()
    assert 'window_expected_branch="auto/window-${occ_repo_slug}-occ-autobind"' in (
        runner
    )


@pytest.mark.unit
def test_conflicted_remint_refuses_a_window_branch() -> None:
    module = _load_script("ci/occ_conflicted_companion_remint.py")
    assert module._BATCH_BRANCH_RE.fullmatch(_WINDOW)


# ---------------------------------------------------------------------------
# The emitter, against a real git origin
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_two_prs_on_different_tickets_share_one_window_companion(
    tmp_path: Path,
) -> None:
    scenario = _WindowScenario(tmp_path)
    emitter = OccCompanionEmitter()
    stack, mocks = scenario.patches(emitter)
    with stack:
        first = scenario.emit(emitter, 101)
        second = scenario.emit(emitter, 102)

    assert "OCC#55" in first
    assert "OCC#55" in second
    # One OCC pull request, one branch, for two tickets.
    assert len(scenario.pull_posts) == 1
    assert _git(
        scenario.origin, "for-each-ref", "--format=%(refname)", "refs/heads/auto"
    ).splitlines() == [f"refs/heads/{_WINDOW}"]
    assert "Evidence-Source: OCC#55" in scenario.bodies[101]
    assert "Evidence-Source: OCC#55" in scenario.bodies[102]
    # The window lease, not a ticket lease, serialises the rebuilds.
    assert mocks["acquire_window"].call_count == 2
    assert mocks["acquire_ticket"].call_count == 0

    for pr_number in (101, 102):
        ticket = _TICKETS[pr_number]
        contract_text = scenario.show(_WINDOW, f"contracts/{ticket}.yaml")
        ids = _contract_ids(contract_text)
        expected = _rendered_member_ids(pr_number) | {"occ-self-bind-pr-55"}
        assert set(ids) == expected
        assert all(ids.count(item) == 1 for item in expected)
        digest = hashlib.sha256(contract_text.encode()).hexdigest()
        receipts = [
            path
            for path in scenario.branch_paths(_WINDOW)
            if path.startswith(f"drift/dod_receipts/{ticket}/")
        ]
        assert receipts
        for path in receipts:
            receipt = yaml.safe_load(scenario.show(_WINDOW, path))
            assert receipt["ticket_id"] == ticket
            assert receipt["contract_sha256"] == f"sha256:{digest}"

    # The OCC PR names every ticket in its title and lists every member.
    pull = scenario.occ_prs[55]
    # Every carried ticket, in a stable order whichever member rebuilt last.
    assert pull["title"] == (
        f"evidence(OMN-11111, OMN-22222): OCC batch window for {_REPO}"
    )
    assert f"- {_REPO}#101" in pull["body"]
    assert f"- {_REPO}#102" in pull["body"]
    assert "Evidence-Ticket: OMN-11111" in pull["body"]
    assert "Evidence-Ticket: OMN-22222" in pull["body"]


@pytest.mark.unit
def test_receipt_gate_passes_for_every_window_member(tmp_path: Path) -> None:
    """The gate is not weakened: each member reads eligible from the window.

    The control is the same two PRs minted one companion each (off): both must
    read eligible there too, so the window proves parity, not a looser gate.
    """
    scenario = _WindowScenario(tmp_path / "window", members=(101, 102, 103))
    emitter = OccCompanionEmitter()
    stack, _mocks = scenario.patches(emitter)
    with stack:
        for pr_number in (101, 102, 103):
            scenario.emit(emitter, pr_number)
    assert len(scenario.pull_posts) == 1
    checkout = scenario.checkout(_WINDOW, tmp_path / "window-checkout")
    for pr_number in (101, 102, 103):
        result = _eligibility(scenario, checkout, pr_number)
        assert result.eligible, (pr_number, result.reason, result.detail)

    # Negative control: a PR that is not a member is not made eligible.
    scenario.bodies[104] = "Evidence-Source: OCC#55\nEvidence-Ticket: OMN-22222\n"
    _TICKETS[104] = "OMN-22222"
    _HEADS[104] = "e" * 40
    try:
        stray = _eligibility(scenario, checkout, 104)
    finally:
        del _TICKETS[104]
        del _HEADS[104]
    assert not stray.eligible

    control = _WindowScenario(tmp_path / "control")
    control_emitter = OccCompanionEmitter()
    control_stack, _ = control.patches(control_emitter)
    with control_stack:
        control.emit(control_emitter, 101, batch_mode=EnumOccBatchMode.OFF)
        control.emit(control_emitter, 102, batch_mode=EnumOccBatchMode.OFF)
    assert len(control.pull_posts) == 2
    for pr_number in (101, 102):
        branch = companion_branch_for(_REPO, pr_number)
        control_checkout = control.checkout(
            branch, tmp_path / f"control-checkout-{pr_number}"
        )
        result = _eligibility(control, control_checkout, pr_number)
        assert result.eligible, (pr_number, result.reason, result.detail)


@pytest.mark.unit
def test_member_change_rebuilds_the_window_in_place(tmp_path: Path) -> None:
    scenario = _WindowScenario(tmp_path)
    emitter = OccCompanionEmitter()
    stack, _mocks = scenario.patches(emitter)
    with stack:
        scenario.emit(emitter, 101)
        scenario.emit(emitter, 102)
        before_a = scenario.member_receipts(_WINDOW, 101)
        before_b = scenario.member_receipts(_WINDOW, 102)
        before_head = scenario.branch_head(_WINDOW)

        # An idempotent re-fire on a healthy window changes nothing.
        assert scenario.emit(emitter, 102).startswith("no-op:")
        assert scenario.branch_head(_WINDOW) == before_head

        # A conflicting window is rebuilt onto the same branch and PR.
        scenario.occ_prs[55]["mergeable"] = False
        scenario.occ_prs[55]["mergeable_state"] = "dirty"
        scenario.emit(emitter, 102)

    assert len(scenario.pull_posts) == 1
    assert scenario.branch_head(_WINDOW) != before_head
    after_a = scenario.member_receipts(_WINDOW, 101)
    after_b = scenario.member_receipts(_WINDOW, 102)
    assert before_a.keys() == after_a.keys()
    for path in before_a:
        assert _without_contract_hashes(before_a[path]) == _without_contract_hashes(
            after_a[path]
        )
    assert before_b.keys() == after_b.keys()
    assert any(before_b[path] != after_b[path] for path in before_b)
    for ticket in ("OMN-11111", "OMN-22222"):
        ids = _contract_ids(scenario.show(_WINDOW, f"contracts/{ticket}.yaml"))
        assert ids.count("occ-self-bind-pr-55") == 1


@pytest.mark.unit
def test_closed_member_is_dropped_and_its_ticket_leaves_the_window(
    tmp_path: Path,
) -> None:
    scenario = _WindowScenario(tmp_path)
    emitter = OccCompanionEmitter()
    stack, _mocks = scenario.patches(emitter)
    with stack:
        scenario.emit(emitter, 101)
        scenario.emit(emitter, 102)
        before_b = scenario.member_receipts(_WINDOW, 102)

        scenario.states[101] = "closed"
        removed = scenario.emit(emitter, 101)
        assert removed.startswith("removed "), removed

        paths = scenario.branch_paths(_WINDOW)
        assert "contracts/OMN-11111.yaml" not in paths
        assert not any(
            path.startswith("drift/dod_receipts/OMN-11111/") for path in paths
        )
        after_b = scenario.member_receipts(_WINDOW, 102)
        assert before_b.keys() == after_b.keys()
        for path in before_b:
            assert _without_contract_hashes(before_b[path]) == _without_contract_hashes(
                after_b[path]
            )
        assert "OMN-11111" not in scenario.occ_prs[55]["title"]
        assert f"{_REPO}#101" not in scenario.occ_prs[55]["body"]

        scenario.states[102] = "closed"
        closed = scenario.emit(emitter, 102)
        assert closed.startswith("closed empty OCC batch"), closed
        assert scenario.occ_prs[55]["state"] == "closed"


@pytest.mark.unit
def test_window_title_names_every_ticket_within_githubs_limit() -> None:
    tickets = [f"OMN-{10000 + n}" for n in range(40)]
    title = OccCompanionEmitter._window_companion_title(_REPO, tickets)
    assert len(title) <= 240
    assert title.startswith("evidence(OMN-10000, ")
    assert " more): OCC batch window for OmniNode-ai/omnimarket" in title
    short = OccCompanionEmitter._window_companion_title(_REPO, ["OMN-1", "OMN-2"])
    assert short == f"evidence(OMN-1, OMN-2): OCC batch window for {_REPO}"
