# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Clone-refusal lock line judging for the host reconcile effect."""

import fcntl
from pathlib import Path

import pytest

from omnimarket.models.model_clone_lock_policy import ModelCloneLockPolicy
from omnimarket.nodes.node_host_reconcile_effect.handlers import (
    handler_host_reconcile_effect as effect,
)
from omnimarket.nodes.node_host_reconcile_effect.handlers.clone_lock_state import (
    describe_lock_line,
    flock_state,
    load_clone_lock_policy,
)
from tests.test_host_reconcile_effect import (
    RecordingEvaluator,
    RecordingPublisher,
    calls,
    command,
    handler,
    setup_workspace,
)

pytestmark = pytest.mark.unit

DECLARED_FLOCK_FILE = "onex-canonical-clone-sync.lock"

POLICY = ModelCloneLockPolicy(persistent_flock_files=(DECLARED_FLOCK_FILE,))


def stale_ref_lock_line(path: Path) -> str:
    """The stale-lock line the verifier prints for ``path``, judged only by age."""
    return (
        f"stale ref lock: {path} age 900s (converge timeout 300s) — "
        f"if no git process is using {path.parent}, remove it with: rm -f {path}"
    )


def test_free_declared_lock_is_not_printed(tmp_path: Path) -> None:
    lock = tmp_path / DECLARED_FLOCK_FILE
    lock.touch()
    assert describe_lock_line(stale_ref_lock_line(lock), POLICY) is None


def test_undeclared_old_lock_is_still_printed(tmp_path: Path) -> None:
    lock = tmp_path / "index.lock"
    lock.touch()
    line = stale_ref_lock_line(lock)
    assert describe_lock_line(line, POLICY) == line


def test_held_declared_lock_is_reported_as_held(tmp_path: Path) -> None:
    lock = tmp_path / DECLARED_FLOCK_FILE
    lock.touch()
    holder = lock.open("rb")
    try:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        assert flock_state(lock) == "held"
        expected = f"lock held by a live process, not stale: {lock}"
        assert describe_lock_line(stale_ref_lock_line(lock), POLICY) == expected
    finally:
        holder.close()


def test_unreadable_declared_lock_is_reported_unreadable(tmp_path: Path) -> None:
    # A path that does not exist fails the probe for every user, root included.
    missing = tmp_path / DECLARED_FLOCK_FILE
    expected = f"lock unreadable, not judged: {missing}"
    assert describe_lock_line(stale_ref_lock_line(missing), POLICY) == expected


def test_unparsable_line_is_printed_as_today() -> None:
    unparsable = f"stale worktree lock: /repo/.git/{DECLARED_FLOCK_FILE} age 1s"
    assert describe_lock_line(unparsable, POLICY) == unparsable
    refused = "refused paths"
    assert describe_lock_line(refused, POLICY) == refused


def test_contract_declares_the_clone_sync_flock_file() -> None:
    assert DECLARED_FLOCK_FILE in load_clone_lock_policy().persistent_flock_files


# The verifier stub from tests.test_host_reconcile_effect with its
# clone-refusal branch extended by two lock lines, as the real script prints
# them for every old *.lock file in the refused clone's git dir: one names the
# declared canonical clone sync flock file (free, nothing holds it), the other
# an undeclared index.lock.
REFUSAL_VERIFIER_STUB = """import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
cmd, args = sys.argv[1], sys.argv[2:]
with (root / "verifier.calls").open("a") as log:
    log.write(json.dumps([cmd, *args]) + "\\n")
def option(name):
    return args[args.index(name) + 1]
if cmd == "clone-health":
    clone = Path(option("--clone"))
    unhealthy = (clone / ".unhealthy").exists()
    print(f"{clone}\\t{'UNHEALTHY' if unhealthy else 'HEALTHY'}\\tbroken index")
    sys.exit(2 if unhealthy else 0)
elif cmd == "clone-refusal":
    print("dirty\\trefused paths")
    print("owner\\towner: lane")
    print("owner\\tconsecutive refusals at this index state: 1")
    print("lock\\t%s")
    print("lock\\t%s")
elif cmd == "lock-targets":
    print(json.dumps({"omnibase-core": "2"}))
elif cmd == "observe":
    file = Path(option("--site-packages")) / "installed-commit"
    print(json.dumps({"commits": {"omnimarket": file.read_text().strip() if file.exists() else ""}}))
elif cmd == "floor":
    Path(option("--output")).write_text(json.dumps({"schema": "floor-stub", "argv": args}) + "\\n")
else:
    raise AssertionError(f"unexpected verifier command: {cmd}")
"""


def write_refusal_verifier(root: Path, declared: Path, undeclared: Path) -> None:
    """Rewrite the stub verifier so its clone-refusal also prints the lock lines."""
    (root / "omnibase_infra/scripts/reconcile_verify_movement.py").write_text(
        REFUSAL_VERIFIER_STUB
        % (stale_ref_lock_line(declared), stale_ref_lock_line(undeclared))
    )


def refused_workspace_with_lock_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A workspace whose omnimarket clone is refused with two stale-lock lines."""
    root = setup_workspace(tmp_path, monkeypatch)
    (root / "omnimarket/.unhealthy").touch()
    fixture = root / "omnimarket/.git-fixture"
    fixture.mkdir()
    (fixture / DECLARED_FLOCK_FILE).touch()
    (fixture / "index.lock").touch()
    write_refusal_verifier(root, fixture / DECLARED_FLOCK_FILE, fixture / "index.lock")
    return root


def test_the_node_prints_no_stale_line_for_a_free_declared_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = refused_workspace_with_lock_lines(tmp_path, monkeypatch)
    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(command(root))
    assert any(call[0] == "clone-refusal" for call in calls(root))
    assert any(
        "stale ref lock" in line and "index.lock" in line for line in result.diagnostics
    )
    assert not any(DECLARED_FLOCK_FILE in line for line in result.diagnostics)


def test_a_policy_without_the_name_keeps_the_stale_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = refused_workspace_with_lock_lines(tmp_path, monkeypatch)
    other_only = ModelCloneLockPolicy(persistent_flock_files=("other.lock",))
    monkeypatch.setattr(effect, "load_clone_lock_policy", lambda: other_only)
    result = handler(RecordingEvaluator(), RecordingPublisher()).handle(command(root))
    assert any(
        "stale ref lock" in line and DECLARED_FLOCK_FILE in line
        for line in result.diagnostics
    )
