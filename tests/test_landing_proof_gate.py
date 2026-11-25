# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing controller merges only a pull request whose lab proof is for the change that merges.

The replay is omnimarket#6765: a Lab comment named commit 3e2244543d, the pull request merged 463773d9dd (98 changed
files against the 25 the Lab commit carried), and a do-not-land message named it. Each refusal has a control that
admits, so a zero is never the reading of a gate that cannot admit anything.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_landing_proof_gate_compute.adapters.policy import (
    load_policy,
)
from omnimarket.nodes.node_landing_proof_gate_compute.adapters.readers import (
    DeadlinePatchIdReader,
    FileProofReader,
    LedgerFileReader,
)
from omnimarket.nodes.node_landing_proof_gate_compute.handlers.handler_landing_proof_gate import (
    HandlerLandingProofGate,
)
from omnimarket.nodes.node_landing_proof_gate_compute.models.model_proof_gate import (
    EnumProofAnswer,
    EnumProofCode,
    ModelLedgerNotice,
    ModelProofGateRequest,
    ModelProofGateResult,
    ModelProofText,
)
from omnimarket.nodes.node_landing_proof_gate_compute.runtime_landing_proof_gate import (
    main as cli_main,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).parent / "fixtures" / "proof_gate"
NODE = "omnimarket.nodes.node_landing_proof_gate_compute"

LAB_HEAD = "3e2244543ddaf3ddbf2b16f0ea0af58837fceb7f"
MERGED_HEAD = "463773d9ddc648dcbcb708552f4004abaf64cd9f"
LAB_PATCH, MERGED_PATCH = "e50c7c90145d", "35e45ed98add"
MSG_ID = "2026-11-24T16:00:51Z-orchestrator"
GRADER = "src/omnimarket/nodes/node_probe_effect/handlers/gateway_checks.py"
PLAIN = "src/omnimarket/nodes/node_x/handlers/handler_x.py"
GOOD_BAD = (
    "Known good answer: the fixed grader passed it (ran).\n"
    "Known bad answer: the grader failed it (ran)."
)

POLICY = load_policy()
REAL = json.loads((FIX / "omnimarket_6765_text.json").read_text())


class Proof:
    def __init__(self, text: ModelProofText | None) -> None:
        self.text = text

    def read(
        self, repo: str, number: int, base: str, head: str
    ) -> ModelProofText | None:
        return self.text


class Ledger:
    def __init__(self, notices: tuple[ModelLedgerNotice, ...] | None = ()) -> None:
        self.notices = notices

    def open_notices(
        self, repo: str, number: int
    ) -> tuple[ModelLedgerNotice, ...] | None:
        return self.notices


class PatchIds:
    def __init__(self, ids: dict[str, str | None]) -> None:
        self.ids = ids

    def patch_id(self, repo: str, base: str, sha: str) -> str | None:
        return self.ids.get(sha)


REPLAY_IDS: dict[str, str | None] = {LAB_HEAD: LAB_PATCH, MERGED_HEAD: MERGED_PATCH}


def gate(
    text: ModelProofText | None,
    notices: tuple[ModelLedgerNotice, ...] | None = (),
    ids: dict[str, str | None] | None = None,
) -> HandlerLandingProofGate:
    return HandlerLandingProofGate(
        POLICY,
        Proof(text),
        Ledger(notices),
        PatchIds(REPLAY_IDS if ids is None else ids),
    )


def text_of(
    *,
    comments: tuple[str, ...] | None = None,
    files: tuple[str, ...] = (GRADER,),
    body: str | None = None,
) -> ModelProofText:
    return ModelProofText(
        body=REAL["body"] if body is None else body,
        comments=tuple(REAL["comments"]) if comments is None else comments,
        files=files,
    )


def request(
    head: str = MERGED_HEAD, number: int = 6765, repo: str = "omnimarket"
) -> ModelProofGateRequest:
    return ModelProofGateRequest(repo=repo, number=number, base="dev", head=head)


def codes(result: ModelProofGateResult) -> list[EnumProofCode]:
    return [r.code for r in result.reasons]


def with_answers(comment: str = GOOD_BAD) -> tuple[str, ...]:
    return (*REAL["comments"], comment)


# AC1 ------------------------------------------------------------------------------------------------------------
def test_the_replay_of_6765_at_its_merged_head_is_draft() -> None:
    r = gate(text_of()).handle(request())
    assert r.answer is EnumProofAnswer.DRAFT
    assert EnumProofCode.LAB_PROOF_OTHER_CHANGE in codes(r)
    flat = " ".join(x.detail for x in r.reasons)
    assert "3e2244543" in flat


def test_control_the_lab_commit_as_the_head_is_proven() -> None:
    r = gate(text_of(comments=with_answers())).handle(request(head=LAB_HEAD))
    assert r.answer is EnumProofAnswer.PROVEN
    assert r.proof == "head"


def test_control_a_head_differing_by_a_base_merge_only_is_proven() -> None:
    same = {**REPLAY_IDS, MERGED_HEAD: LAB_PATCH}
    r = gate(text_of(comments=with_answers()), ids=same).handle(request())
    assert r.answer is EnumProofAnswer.PROVEN
    assert r.proof == "patch_id"


def test_no_lab_line_is_draft_naming_the_missing_item() -> None:
    r = gate(text_of(comments=(), files=(PLAIN,))).handle(request())
    assert codes(r) == [EnumProofCode.LAB_LINE_MISSING]
    assert "Lab line naming a commit" in r.reasons[0].item


def test_a_lab_heading_with_no_commit_is_not_proof() -> None:
    assert "## Lab" in REAL["body"]
    r = gate(text_of(comments=(), files=(PLAIN,))).handle(request())
    assert codes(r) == [EnumProofCode.LAB_LINE_MISSING]


# fail closed (decision c) -----------------------------------------------------------------------------------------
def test_an_unreadable_proof_is_draft_naming_proof_unreadable() -> None:
    r = gate(None).handle(request())
    assert r.answer is EnumProofAnswer.DRAFT
    assert codes(r) == [EnumProofCode.PROOF_UNREADABLE]
    assert "proof unreadable" in r.reasons[0].detail


def test_an_unreadable_patch_id_is_draft() -> None:
    r = gate(text_of(files=(PLAIN,)), ids={LAB_HEAD: LAB_PATCH}).handle(request())
    assert codes(r) == [EnumProofCode.PROOF_UNREADABLE]


def test_an_unreadable_ledger_is_draft() -> None:
    r = gate(text_of(comments=with_answers()), notices=None).handle(
        request(head=LAB_HEAD)
    )
    assert codes(r) == [EnumProofCode.PROOF_UNREADABLE]
    assert r.reasons[0].item == "ledger"


def test_a_changed_file_list_that_is_empty_is_unreadable() -> None:
    r = gate(text_of(files=())).handle(request(head=LAB_HEAD))
    assert codes(r) == [EnumProofCode.PROOF_UNREADABLE]


# AC2 ----------------------------------------------------------------------------------------------------------------
def test_a_grader_change_with_unit_tests_only_is_draft() -> None:
    files = (GRADER, "tests/nodes/test_gateway_checks.py")
    r = gate(text_of(files=files)).handle(request(head=LAB_HEAD))
    assert codes(r) == [
        EnumProofCode.KNOWN_GOOD_MISSING,
        EnumProofCode.KNOWN_BAD_MISSING,
    ]


def test_a_grader_change_naming_one_answer_is_draft_for_the_other() -> None:
    comment = "Known good answer: the fixed grader passed it."
    r = gate(text_of(comments=with_answers(comment))).handle(request(head=LAB_HEAD))
    assert codes(r) == [EnumProofCode.KNOWN_BAD_MISSING]


def test_control_a_non_grader_change_needs_no_known_answers() -> None:
    r = gate(text_of(files=(PLAIN,))).handle(request(head=LAB_HEAD))
    assert r.answer is EnumProofAnswer.PROVEN


def test_control_a_grader_change_naming_both_answers_is_proven() -> None:
    r = gate(text_of(comments=with_answers())).handle(request(head=LAB_HEAD))
    assert r.answer is EnumProofAnswer.PROVEN


# AC3 (decision a: the answer is DRAFT, not a hold state) ----------------------------------------------------------
def _ledger(tmp_path: Path, *rows: str) -> Path:
    path = tmp_path / "rows.md"
    path.write_text("\n".join(rows) + "\n")
    return path


def _msg() -> str:
    return (FIX / "ledger_do_not_land_6765.txt").read_text().strip()


def test_the_do_not_land_message_makes_the_answer_draft_whatever_is_green(
    tmp_path: Path,
) -> None:
    reader = LedgerFileReader(POLICY, str(_ledger(tmp_path, _msg())))
    notices = reader.open_notices("omnimarket", 6765)
    assert notices is not None
    assert [(n.row_id, n.rtype) for n in notices] == [(MSG_ID, "MSG")]
    r = gate(text_of(comments=with_answers()), notices=notices).handle(
        request(head=LAB_HEAD)
    )
    assert r.answer is EnumProofAnswer.DRAFT
    assert codes(r) == [EnumProofCode.DO_NOT_LAND]
    assert r.reasons[0].item == MSG_ID


def test_a_release_naming_the_message_lifts_it(tmp_path: Path) -> None:
    release = (
        f"2026-11-24T22:00:00Z | RELEASE | lane=orchestrator | re={MSG_ID} | allowed"
    )
    reader = LedgerFileReader(POLICY, str(_ledger(tmp_path, _msg(), release)))
    assert reader.open_notices("omnimarket", 6765) == ()


def test_the_message_names_only_its_own_pull_request(tmp_path: Path) -> None:
    reader = LedgerFileReader(POLICY, str(_ledger(tmp_path, _msg())))
    assert reader.open_notices("omnimarket", 6766) == ()
    assert reader.open_notices("omnimarket", 676) == ()
    assert reader.open_notices("omnibase_infra", 6765) == ()


def test_an_open_hold_row_counts_the_same_as_the_message(tmp_path: Path) -> None:
    hold = (
        "2026-11-24T20:00:00Z | HOLD | lane=x | id=hold-1 | pr=OmniNode-ai/omnimarket#6765 "
        "| until=2999-01-01T00:00:00Z | wait for the proof"
    )
    now = datetime(2026, 11, 24, 21, tzinfo=UTC)
    path = _ledger(tmp_path, hold)
    got = LedgerFileReader(POLICY, str(path), now=lambda: now).open_notices(
        "omnimarket", 6765
    )
    assert got is not None
    assert [n.rtype for n in got] == ["HOLD"]
    path = _ledger(
        tmp_path, hold, "2026-11-24T21:00:00Z | RELEASE | lane=x | re=hold-1 | done"
    )
    released = LedgerFileReader(POLICY, str(path), now=lambda: now)
    assert released.open_notices("omnimarket", 6765) == ()


def test_a_missing_ledger_is_unreadable_never_empty(tmp_path: Path) -> None:
    reader = LedgerFileReader(POLICY, str(tmp_path / "absent.md"))
    assert reader.open_notices("omnimarket", 6765) is None


# AC4 ----------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("files", "kind"),
    [
        (("docs/plans/x.md", "README.md"), "docs"),
        (("tests/test_file_durations.json",), "test_durations"),
        (("docs/x.md", "tests/test_file_durations.json"), "docs+test_durations"),
    ],
)
def test_documentation_only_and_duration_only_changes_need_no_lab_line(
    files: tuple[str, ...], kind: str
) -> None:
    r = gate(text_of(comments=(), files=files)).handle(request())
    assert r.answer is EnumProofAnswer.PROVEN
    assert r.exempt == kind


@pytest.mark.parametrize(
    "files",
    [("docs/x.md", PLAIN), ("docs/helper.py",), ("tests/test_file_durations.py",)],
)
def test_control_a_python_file_is_never_exempt(files: tuple[str, ...]) -> None:
    r = gate(text_of(comments=(), files=files)).handle(request())
    assert codes(r) == [EnumProofCode.LAB_LINE_MISSING]


def test_an_exempt_change_under_a_do_not_land_is_still_draft() -> None:
    notice = ModelLedgerNotice(row_id="m", rtype="MSG", ts="2026-11-24T16:00:51Z")
    r = gate(text_of(comments=(), files=("docs/x.md",)), notices=(notice,)).handle(
        request()
    )
    assert r.answer is EnumProofAnswer.DRAFT
    assert codes(r) == [EnumProofCode.DO_NOT_LAND]


# typed output and the command -------------------------------------------------------------------------------------
def _pr_text(
    tmp_path: Path, comments: list[str], files: list[str] | None = None
) -> Path:
    path = tmp_path / "pr_text.json"
    path.write_text(
        json.dumps({"body": "", "comments": comments, "files": files or [PLAIN]})
    )
    return path


def _run_cli(
    capsys: pytest.CaptureFixture[str], *argv: str
) -> tuple[int, dict[str, object]]:
    code = cli_main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_the_command_prints_proven_as_typed_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pr = _pr_text(tmp_path, [f"Lab: head={LAB_HEAD}"])
    ledger = _ledger(tmp_path, "2026-11-24T10:00:00Z | STATUS | lane=x | nothing")
    code, out = _run_cli(
        capsys, "--repo", "omnimarket", "--number", "6765", "--base", "dev",
        "--head", LAB_HEAD, "--pr-text-file", str(pr), "--ledger-path", str(ledger),
    )  # fmt: skip
    assert (code, out["answer"], out["reasons"]) == (0, "PROVEN", [])


def test_the_command_prints_draft_with_the_reason_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pr = _pr_text(tmp_path, [f"Lab: head={LAB_HEAD}"])
    ledger = _ledger(tmp_path, _msg())
    code, out = _run_cli(
        capsys, "--repo", "omnimarket", "--number", "6765", "--base", "dev",
        "--head", LAB_HEAD, "--pr-text-file", str(pr), "--ledger-path", str(ledger),
    )  # fmt: skip
    assert code == 1
    assert out["answer"] == "DRAFT"
    assert out["reasons"][0]["code"] == "do_not_land"  # type: ignore[index]


def test_the_command_with_no_pr_text_is_draft_proof_unreadable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = _ledger(tmp_path, "2026-11-24T10:00:00Z | STATUS | lane=x | nothing")
    code, out = _run_cli(
        capsys, "--repo", "omnimarket", "--number", "6765", "--base", "dev",
        "--head", LAB_HEAD, "--pr-text-file", str(tmp_path / "absent.json"),
        "--ledger-path", str(ledger),
    )  # fmt: skip
    assert code == 1
    assert out["reasons"][0]["detail"] == "proof unreadable"  # type: ignore[index]


def test_the_command_without_the_ledger_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pr = _pr_text(tmp_path, [f"Lab: head={LAB_HEAD}"])
    code, out = _run_cli(
        capsys, "--repo", "omnimarket", "--number", "6765", "--base", "dev",
        "--head", LAB_HEAD, "--pr-text-file", str(pr), "--no-ledger",
    )  # fmt: skip
    assert (code, out["answer"], out["ledger_checked"]) == (0, "PROVEN", False)


def test_the_command_answers_within_its_budget_when_a_read_hangs() -> None:
    clock = iter([0.0, 0.0, 6.0, 6.0, 6.0])
    reader = DeadlinePatchIdReader(
        PatchIds({"a": "x"}), budget_s=5.0, now=lambda: next(clock)
    )
    assert reader.patch_id("omnimarket", "dev", "a") == "x"
    assert reader.patch_id("omnimarket", "dev", "a") is None


def test_the_file_reader_reads_body_comments_and_files(tmp_path: Path) -> None:
    path = _pr_text(tmp_path, ["c1"], ["a.py"])
    got = FileProofReader(str(path)).read("omnimarket", 1, "dev", "x")
    assert got is not None
    assert (got.comments, got.files) == (("c1",), ("a.py",))
    assert FileProofReader(str(tmp_path / "absent.json")).read("o", 1, "d", "x") is None


def test_the_command_runs_as_a_module(tmp_path: Path) -> None:
    pr = _pr_text(tmp_path, [f"Lab: head={LAB_HEAD}"])
    done = subprocess.run(
        [sys.executable, "-m", NODE, "--repo", "omnimarket", "--number", "1",
         "--base", "dev", "--head", LAB_HEAD, "--pr-text-file", str(pr), "--no-ledger"],
        capture_output=True, text=True, check=False, cwd=ROOT,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["answer"] == "PROVEN"


# AC5: the replay of the last 20 merged pull requests -------------------------------------------------------------
def _replay() -> list[dict[str, object]]:
    rows = json.loads((FIX / "replay_last20.json").read_text())
    assert isinstance(rows, list)
    return rows


def _replay_one(row: dict[str, object]) -> ModelProofGateResult:
    ids = row["patch_ids"]
    assert isinstance(ids, dict)
    text = ModelProofText(
        body="\n".join(row["text_lines"]),  # type: ignore[arg-type]
        comments=(),
        files=tuple(row["files"]),  # type: ignore[arg-type]
    )
    return gate(text, ids=ids).handle(
        ModelProofGateRequest(
            repo="omnimarket",
            number=int(str(row["pr"])),
            base="dev",
            head=str(row["head"]),
        )
    )


def test_the_replay_covers_twenty_merged_pull_requests() -> None:
    assert len(_replay()) == 20


def test_the_replay_matches_the_recorded_answer_of_every_pull_request() -> None:
    for row in _replay():
        got = _replay_one(row)
        expect = row["expect"]
        assert isinstance(expect, dict)
        assert got.answer.value == expect["answer"], row["pr"]
        assert sorted(c.value for c in codes(got)) == expect["codes"], row["pr"]


def test_the_replay_counts_sixteen_refused_with_their_reasons() -> None:
    results = [_replay_one(r) for r in _replay()]
    refused = [r for r in results if r.answer is EnumProofAnswer.DRAFT]
    assert len(refused) == 16
    by_code = Counter(c.value for r in refused for c in codes(r))
    assert dict(by_code) == {
        "lab_proof_other_change": 12,
        "lab_line_missing": 3,
        "known_good_missing": 4,
        "known_bad_missing": 4,
    }
    proven = [r for r in results if r.answer is EnumProofAnswer.PROVEN]
    assert Counter(r.proof for r in proven) == {"head": 3, "patch_id": 1}


# contract, metadata and registration ---------------------------------------------------------------------------
def test_the_node_is_declared_and_registered() -> None:
    node = ROOT / "src/omnimarket/nodes/node_landing_proof_gate_compute"
    contract = yaml.safe_load((node / "contract.yaml").read_text())
    assert contract["name"] == "node_landing_proof_gate_compute"
    assert contract["handler"]["class"] == "HandlerLandingProofGate"
    meta = yaml.safe_load((node / "metadata.yaml").read_text())
    assert meta["name"] == "node_landing_proof_gate_compute"
    assert meta["capabilities"]["requires_network"] is False
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert (
        'node_landing_proof_gate_compute = "omnimarket.nodes.node_landing_proof_gate_compute"'
        in pyproject
    )


def test_the_handler_names_no_path_host_or_address() -> None:
    node = ROOT / "src/omnimarket/nodes/node_landing_proof_gate_compute"
    banned = ("/data/", "/home/", "/Users/", "http://", "https://", '"gh"', "'gh'")
    for path in node.rglob("*.py"):
        text = path.read_text()
        for word in banned:
            assert word not in text, f"{path.name} holds {word}"
