# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reject-skip-token compute and its remote pre-commit export.

The node must refuse exactly what the vendored ``reject-deploy-gate-skip-token.sh`` refuses (Rule 10):
every ``[skip-<letter>`` token in staged PR-body-like files, committed session evidence, a commit
message or a PR body, unless the same text carries ``# skip-token-allowed: <receipt-id>``.
"""

from __future__ import annotations

import inspect
import itertools
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_reject_skip_token_compute import (
    HandlerRejectSkipToken,
    NodeRejectSkipTokenCompute,
)
from omnimarket.nodes.node_reject_skip_token_compute.models.model_skip_token_scan import (
    EnumSkipTokenSurface,
    EnumSkipTokenVerdict,
    ModelSkipTokenScanItem,
    ModelSkipTokenScanRequest,
    ModelSkipTokenScanResult,
)

REPO = Path(__file__).resolve().parents[4]
NODE_DIR = REPO / "src" / "omnimarket" / "nodes" / "node_reject_skip_token_compute"
HOOK_SCRIPT = REPO / ".pre-commit-hooks" / "reject-deploy-gate-skip-token.sh"
HOOK_EXPORT = REPO / ".pre-commit-hooks.yaml"

TOKEN = "[skip-" + "deploy-gate: correctness fix]"
ALLOW = "# skip-token-allowed: USER-APPROVAL-2026-04-25-test"


def _scan(*items: ModelSkipTokenScanItem) -> ModelSkipTokenScanResult:
    return HandlerRejectSkipToken().handle(ModelSkipTokenScanRequest(items=items))


def _staged(path: str, text: str) -> ModelSkipTokenScanItem:
    return ModelSkipTokenScanItem(
        surface=EnumSkipTokenSurface.STAGED_FILE, path=path, text=text
    )


def _message(text: str) -> ModelSkipTokenScanItem:
    return ModelSkipTokenScanItem(
        surface=EnumSkipTokenSurface.COMMIT_MESSAGE, path="COMMIT_EDITMSG", text=text
    )


def _body(text: str) -> ModelSkipTokenScanItem:
    return ModelSkipTokenScanItem(
        surface=EnumSkipTokenSurface.PR_BODY, path="pr-body", text=text
    )


# (name, relative path, text, blocked)
STAGED_CASES: list[tuple[str, str, str, bool]] = [
    ("clean markdown", "docs/a.md", "No bypass tokens here.\n", False),
    ("deploy-gate token", "docs/a.md", TOKEN + "\n", True),
    ("receipt-gate token", "x.yaml", "[skip-receipt-gate: docs only]\n", True),
    ("any other skip token", "x.yml", "[skip-anything: some reason]\n", True),
    ("upper case token", "x.txt", "[Skip-Deploy-Gate: reason here]\n", True),
    ("token with allowlist receipt", "a.md", TOKEN + "\n" + ALLOW + "\n", False),
    (
        "allowlist receipt in upper case",
        "a.md",
        TOKEN + "\n# SKIP-TOKEN-ALLOWED: RCPT-1\n",
        False,
    ),
    ("allowlist without a token", "a.md", "Body\n" + ALLOW + "\n", False),
    (
        "allowlist with empty receipt id",
        "a.md",
        TOKEN + "\n# skip-token-allowed:\n",
        True,
    ),
    (
        "allowlist whose id is on the next line",
        "a.md",
        TOKEN + "\n# skip-token-allowed:\nRCPT-1\n",
        True,
    ),
    (
        "allowlist spaced after the hash",
        "a.md",
        TOKEN + "\n#   skip-token-allowed:   RCPT-1\n",
        False,
    ),
    (
        "allowlist missing its hash",
        "a.md",
        TOKEN + "\nskip-token-allowed: RCPT-1\n",
        True,
    ),
    ("skip followed by a digit is not a token", "a.md", "[skip-1 thing]\n", False),
    ("skip with no letter is not a token", "a.md", "[skip- thing]\n", False),
    ("bracket missing", "a.md", "skip-deploy-gate: x\n", False),
    ("python source is out of scope", "src/a.py", TOKEN + "\n", False),
    ("shell source is out of scope", "scripts/a.sh", TOKEN + "\n", False),
    ("upper case extension is out of scope", "a.MD", TOKEN + "\n", False),
    ("evidence json is in scope", ".onex_state/evidence/s.json", TOKEN + "\n", True),
    ("nested evidence jsonl", "x/.onex_state/evidence/s.jsonl", TOKEN + "\n", True),
    ("evidence log", ".onex_state/evidence/s.log", TOKEN + "\n", True),
    ("evidence err", ".onex_state/evidence/s.err", TOKEN + "\n", True),
    ("evidence out", ".onex_state/evidence/s.out", TOKEN + "\n", True),
    ("evidence markdown", ".onex_state/evidence/s.md", TOKEN + "\n", True),
    ("evidence py is out of scope", ".onex_state/evidence/s.py", TOKEN + "\n", False),
    ("evidence bin is out of scope", ".onex_state/evidence/s.bin", TOKEN + "\n", False),
    ("json outside evidence is out of scope", "other/s.json", TOKEN + "\n", False),
    (
        "evidence json with allowlist receipt",
        ".onex_state/evidence/s.json",
        TOKEN + "\n" + ALLOW + "\n",
        False,
    ),
    ("token on the last line without newline", "a.md", "x\n" + TOKEN, True),
    ("token mid line", "a.md", "see " + TOKEN + " for details\n", True),
]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "path", "text", "blocked"), STAGED_CASES, ids=[c[0] for c in STAGED_CASES]
)
def test_staged_file_verdict(name: str, path: str, text: str, blocked: bool) -> None:
    result = _scan(_staged(path, text))
    expected = EnumSkipTokenVerdict.BLOCK if blocked else EnumSkipTokenVerdict.PASS
    assert result.verdict is expected, name


@pytest.mark.unit
def test_commit_message_is_scanned_whatever_its_name() -> None:
    assert _scan(_message("fix: thing\n\n" + TOKEN + "\n")).verdict is (
        EnumSkipTokenVerdict.BLOCK
    )
    assert _scan(_message("fix: thing\n\n" + TOKEN + "\n" + ALLOW + "\n")).verdict is (
        EnumSkipTokenVerdict.PASS
    )
    assert _scan(_message("fix: clean\n")).verdict is EnumSkipTokenVerdict.PASS


@pytest.mark.unit
def test_pr_body_is_scanned() -> None:
    assert _scan(_body(TOKEN)).verdict is EnumSkipTokenVerdict.BLOCK
    assert _scan(_body(TOKEN + "\n" + ALLOW)).verdict is EnumSkipTokenVerdict.PASS


@pytest.mark.unit
def test_findings_name_each_blocked_path_and_each_allowed_path() -> None:
    result = _scan(
        _staged("a.md", TOKEN),
        _staged("b.md", TOKEN + "\n" + ALLOW),
        _staged("c.md", "clean"),
        _staged("d.py", TOKEN),
    )
    assert result.verdict is EnumSkipTokenVerdict.BLOCK
    assert [(f.path, f.allowed) for f in result.findings] == [
        ("a.md", False),
        ("b.md", True),
    ]
    assert result.scanned == 3
    assert result.out_of_scope == ("d.py",)


@pytest.mark.unit
def test_one_blocked_item_blocks_the_request_among_many_clean() -> None:
    result = _scan(*[_staged(f"{i}.md", "clean") for i in range(5)], _body(TOKEN))
    assert result.verdict is EnumSkipTokenVerdict.BLOCK


@pytest.mark.unit
def test_empty_request_passes_with_nothing_scanned() -> None:
    result = _scan()
    assert result.verdict is EnumSkipTokenVerdict.PASS
    assert result.scanned == 0


@pytest.mark.unit
def test_handler_is_pure_and_deterministic() -> None:
    items = (_staged("a.md", TOKEN), _body("clean"))
    assert _scan(*items) == _scan(*items)


# ---------------------------------------------------------------------------
# Parity with the vendored script, which is what the remote hook runs.
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env=scrub_git_location_env(os.environ),
    )


def _run_script(cwd: Path, *args: str) -> int:
    env = scrub_git_location_env(os.environ)
    env.pop("GIT_HOOK_STAGE", None)
    return subprocess.run(
        ["bash", str(HOOK_SCRIPT), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    ).returncode


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "t")
    return tmp_path


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "path", "text", "blocked"), STAGED_CASES, ids=[c[0] for c in STAGED_CASES]
)
def test_staged_file_script_parity(
    repo: Path, name: str, path: str, text: str, blocked: bool
) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _git(repo, "add", "-f", path)
    rc = _run_script(repo, path)
    node = _scan(_staged(path, text)).verdict is EnumSkipTokenVerdict.BLOCK
    assert (rc != 0) is blocked, f"script disagrees with the table: {name}"
    assert node is (rc != 0), f"node disagrees with the script: {name}"


@pytest.mark.unit
def test_script_reads_the_index_not_the_working_tree(repo: Path) -> None:
    target = repo / "a.md"
    target.write_text(TOKEN + "\n", encoding="utf-8")
    _git(repo, "add", "a.md")
    target.write_text("clean now\n", encoding="utf-8")
    assert _run_script(repo, "a.md") != 0


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "fix: x\n\n" + TOKEN + "\n",
        "fix: x\n\n" + TOKEN + "\n" + ALLOW + "\n",
        "fix: clean\n",
        "[Skip-Receipt-Gate: x]\n",
    ],
)
def test_commit_message_script_parity(repo: Path, text: str) -> None:
    message = repo / ".git" / "COMMIT_EDITMSG"
    message.write_text(text, encoding="utf-8")
    heuristic = _run_script(repo, str(message))
    explicit = _run_script(repo, "--commit-msg", str(message))
    node = _scan(_message(text)).verdict is EnumSkipTokenVerdict.BLOCK
    assert (heuristic != 0) is node
    assert (explicit != 0) is node


@pytest.mark.unit
def test_commit_message_flag_scans_a_message_file_with_any_name(repo: Path) -> None:
    message = repo / "msg.txt"
    message.write_text(TOKEN + "\n", encoding="utf-8")
    assert _run_script(repo, "--commit-msg", str(message)) != 0


@pytest.mark.unit
def test_script_self_test_passes(repo: Path) -> None:
    assert _run_script(repo, "--self-test") == 0


@pytest.mark.unit
def test_script_scan_is_bash_here_string_free() -> None:
    assert "<<<" not in HOOK_SCRIPT.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The export: .pre-commit-hooks.yaml consumed by repo:/rev:.
# ---------------------------------------------------------------------------


def _export() -> dict[str, dict[str, object]]:
    hooks = yaml.safe_load(HOOK_EXPORT.read_text(encoding="utf-8"))
    return {h["id"]: h for h in hooks}


@pytest.mark.unit
def test_export_declares_both_stages() -> None:
    hooks = _export()
    assert set(hooks) == {"reject-skip-token", "reject-skip-token-commit-msg"}
    staged = hooks["reject-skip-token"]
    assert staged["stages"] == ["pre-commit"]
    assert staged["language"] == "script"
    assert staged["pass_filenames"] is True
    assert staged["always_run"] is True
    message = hooks["reject-skip-token-commit-msg"]
    assert message["stages"] == ["commit-msg"]
    assert message["language"] == "script"
    assert message["pass_filenames"] is True
    assert message["always_run"] is True


@pytest.mark.unit
def test_export_entries_are_self_contained_scripts_in_this_repo() -> None:
    for hook in _export().values():
        entry = str(hook["entry"]).split()[0]
        assert entry == ".pre-commit-hooks/reject-deploy-gate-skip-token.sh"
        assert (REPO / entry).is_file()
        assert os.access(REPO / entry, os.X_OK)


@pytest.mark.unit
def test_script_sources_nothing_and_imports_nothing() -> None:
    body = HOOK_SCRIPT.read_text(encoding="utf-8")
    code = [ln for ln in body.splitlines() if not ln.lstrip().startswith("#")]
    joined = "\n".join(code)
    assert "source " not in joined
    assert "\n. " not in joined
    assert "python" not in joined
    assert "$OMNI_HOME" not in joined


@pytest.mark.unit
def test_pre_commit_try_repo_runs_both_stages(tmp_path: Path) -> None:
    """The plan's step 5: a real pre-commit run of each exported id, not just the YAML."""
    pre_commit = shutil.which("pre-commit")
    assert pre_commit is not None, "pre-commit is a dev dependency of this repo"
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    _git(consumer, "init", "-q")
    _git(consumer, "config", "user.email", "t@example.invalid")
    _git(consumer, "config", "user.name", "t")
    # try-repo clones the hook repo at HEAD, so give it a committed copy of this working tree's hooks.
    hook_repo = tmp_path / "hook-repo"
    (hook_repo / ".pre-commit-hooks").mkdir(parents=True)
    shutil.copy(HOOK_EXPORT, hook_repo / ".pre-commit-hooks.yaml")
    shutil.copy(HOOK_SCRIPT, hook_repo / ".pre-commit-hooks" / HOOK_SCRIPT.name)
    _git(hook_repo, "init", "-q")
    _git(hook_repo, "config", "user.email", "t@example.invalid")
    _git(hook_repo, "config", "user.name", "t")
    _git(hook_repo, "add", "-A")
    _git(hook_repo, "commit", "-q", "-m", "hooks")

    bad = consumer / "notes.md"
    bad.write_text(TOKEN + "\n", encoding="utf-8")
    _git(consumer, "add", "notes.md")
    staged = subprocess.run(
        [
            pre_commit,
            "try-repo",
            str(hook_repo),
            "reject-skip-token",
            "--files",
            "notes.md",
        ],
        cwd=consumer,
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert staged.returncode != 0, staged.stdout + staged.stderr

    bad.write_text("clean\n", encoding="utf-8")
    _git(consumer, "add", "notes.md")
    clean = subprocess.run(
        [
            pre_commit,
            "try-repo",
            str(hook_repo),
            "reject-skip-token",
            "--files",
            "notes.md",
        ],
        cwd=consumer,
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert clean.returncode == 0, clean.stdout + clean.stderr

    message = consumer / ".git" / "COMMIT_EDITMSG"
    message.write_text("fix: x\n\n" + TOKEN + "\n", encoding="utf-8")
    refused = subprocess.run(
        [
            pre_commit,
            "try-repo",
            str(hook_repo),
            "reject-skip-token-commit-msg",
            "--hook-stage",
            "commit-msg",
            "--commit-msg-filename",
            str(message),
        ],
        cwd=consumer,
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert refused.returncode != 0, refused.stdout + refused.stderr

    message.write_text("fix: x\n", encoding="utf-8")
    accepted = subprocess.run(
        [
            pre_commit,
            "try-repo",
            str(hook_repo),
            "reject-skip-token-commit-msg",
            "--hook-stage",
            "commit-msg",
            "--commit-msg-filename",
            str(message),
        ],
        cwd=consumer,
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr


# ---------------------------------------------------------------------------
# The contract.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_contract_declares_a_pure_compute_with_topics() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["name"] == "node_reject_skip_token_compute"
    assert contract["node_type"] == "COMPUTE_GENERIC"
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["input_model"]["name"] == "ModelSkipTokenScanRequest"
    assert contract["output_model"]["name"] == "ModelSkipTokenScanResult"
    dispatch = contract["runtime_dispatch"]
    assert dispatch["command_topic"].startswith("onex.cmd.omnimarket.")
    for topic in dispatch["terminal_events"].values():
        assert topic.startswith("onex.evt.omnimarket.")
    handler = contract["handler"]
    assert handler["class"] == "HandlerRejectSkipToken"
    assert handler["module"].endswith("handler_reject_skip_token")


@pytest.mark.unit
def test_handler_has_the_canonical_signature() -> None:
    sig = inspect.signature(HandlerRejectSkipToken.handle)
    params = [p for p in sig.parameters if p != "self"]
    assert params == ["request"]
    assert NodeRejectSkipTokenCompute.handle is HandlerRejectSkipToken.handle
    hints = inspect.get_annotations(HandlerRejectSkipToken.handle, eval_str=True)
    assert hints["request"] is ModelSkipTokenScanRequest
    assert hints["return"] is ModelSkipTokenScanResult


@pytest.mark.unit
def test_node_is_registered_as_an_entry_point() -> None:
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert (
        'node_reject_skip_token_compute = "omnimarket.nodes.node_reject_skip_token_compute"'
        in pyproject
    )


@pytest.mark.unit
def test_node_module_imports_nothing_outside_the_standard_library_and_models() -> None:
    import ast

    allowed_prefixes = ("omnimarket.nodes.node_reject_skip_token_compute", "pydantic")
    stdlib = {
        "__future__",
        "enum",
        "re",
        "typing",
        "collections",
        "dataclasses",
    }
    for path in itertools.chain(
        (NODE_DIR / "handlers").glob("*.py"), (NODE_DIR / "models").glob("*.py")
    ):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for name in names:
                root = name.split(".")[0]
                assert root in stdlib or name.startswith(allowed_prefixes), (
                    f"{path.name} imports {name}"
                )
