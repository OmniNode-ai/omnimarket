# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One venv per lock hash for the lab plugin store (OMN-22740): golden chain, error chain, never-rebuilt and prune.

The uv in these tests is a small stand-in that makes a real venv with one dependency package (``sync``) and a real editable
``.pth`` (``pip install --editable``), so what is read is what the tree's interpreter imports. The same operations run with
the real uv on the lab hosts; the pull request carries those readings.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_remote_lane_effect.handlers import (
    HandlerRemoteLaneSharedVenv,
    HandlerRemoteLaneSharedVenvPrune,
)
from omnimarket.nodes.node_remote_lane_effect.models import (
    ModelRemoteLaneSharedVenvPruneRequest,
    ModelRemoteLaneSharedVenvRequest,
)
from omnimarket.nodes.node_remote_lane_effect.plugin_shared_venv import (
    MARKER,
    READY,
    count_files,
    lock_key,
    shared_venv_prune_script,
    shared_venv_script,
)

FAKE_UV = """#!{python}
import os, subprocess, sys, venv
from pathlib import Path

log = os.environ.get("FAKE_UV_LOG")
args = sys.argv[1:]
if log:
    with open(log, "a") as f:
        f.write(" ".join(args[:2]) + "\\n")
if os.environ.get("FAKE_UV_FAIL") == args[0]:
    sys.stderr.write("injected break: " + args[0] + "\\n")
    sys.exit(1)
if args[0] == "sync":
    target = Path(os.environ["UV_PROJECT_ENVIRONMENT"])
    venv.create(target, with_pip=False, symlinks=True)
    site = next((target / "lib").glob("python*/site-packages"))
    (site / "dep_pkg").mkdir()
    (site / "dep_pkg" / "__init__.py").write_text("VALUE = 7\\n")
    (site / "dep_pkg-1.0.dist-info").mkdir()
    (site / "dep_pkg-1.0.dist-info" / "METADATA").write_text("Name: dep-pkg\\nVersion: 1.0\\n")
    (site / "dep.pth").write_text("# a shared pth file\\n")
    sys.exit(0)
if args[:2] == ["pip", "install"]:
    py = args[args.index("--python") + 1]
    tree = Path(args[args.index("--editable") + 1])
    site = Path(subprocess.check_output(
        [py, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"], text=True).strip())
    (site / "_editable_proj.pth").write_text(str(tree / "src") + "\\n")
    (site / "proj-0.1.dist-info").mkdir()
    (site / "proj-0.1.dist-info" / "METADATA").write_text("Name: proj\\nVersion: 0.1\\n")
    sys.exit(0)
sys.exit(2)
"""


@pytest.fixture
def uv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    script = bin_dir / "uv"
    script.write_text(FAKE_UV.format(python=sys.executable))
    script.chmod(0o755)
    (bin_dir / "python3").symlink_to(sys.executable)
    monkeypatch.setenv("FAKE_UV_LOG", str(tmp_path / "uv.log"))
    monkeypatch.delenv("FAKE_UV_FAIL", raising=False)
    return script


@pytest.fixture
def store(tmp_path: Path) -> Path:
    root = tmp_path / "lab-run"
    (root / "plugins").mkdir(parents=True)
    return root


def _tree(store: Path, name: str, lock: str = "lock one\n") -> Path:
    tree = store / "plugins" / name
    (tree / "src" / "proj").mkdir(parents=True)
    (tree / "src" / "proj" / "__init__.py").write_text(f"NAME = {name!r}\n")
    (tree / "uv.lock").write_text(lock)
    (tree / "pyproject.toml").write_text('[project]\nname = "proj"\n')
    return tree


def _request(
    tree: Path, store: Path, uv: Path, **more: object
) -> ModelRemoteLaneSharedVenvRequest:
    return ModelRemoteLaneSharedVenvRequest(
        tree=str(tree), store_root=str(store), uv=str(uv), build_timeout_s=60, **more
    )


def _imports(tree: Path) -> str:
    done = subprocess.run(
        [
            str(tree / ".venv" / "bin" / "python3"),
            "-c",
            "import dep_pkg, proj, sys; print(dep_pkg.VALUE, proj.NAME, proj.__file__)",
        ],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": os.environ["PATH"]},
    )
    return done.stdout.strip()


def _log(tmp_path: Path) -> list[str]:
    path = tmp_path / "uv.log"
    return path.read_text().splitlines() if path.exists() else []


def _snapshot(root: Path) -> dict[str, float]:
    return {str(p): p.lstat().st_mtime for p in sorted(root.rglob("*"))}


# ---- golden chain ---------------------------------------------------------------------------------


def test_golden_a_tree_reaches_the_shared_venv_and_imports_its_own_project(
    uv: Path, store: Path, tmp_path: Path
) -> None:
    tree = _tree(store, "omniclaude-aaaaaaaaaaaa")
    result = HandlerRemoteLaneSharedVenv().handle(_request(tree, store, uv))
    assert result.ok
    assert result.built
    assert result.linked
    assert result.refusal is None
    assert result.key == lock_key(tree / "uv.lock")
    shared = store / "venvs" / result.key
    assert (shared / READY).is_file()
    assert json.loads((tree / ".venv" / MARKER).read_text())["key"] == result.key
    assert _imports(tree) == f"7 omniclaude-aaaaaaaaaaaa {tree}/src/proj/__init__.py"
    assert result.tree_venv_files == count_files(tree / ".venv") < 30
    assert _log(tmp_path).count("sync --frozen") == 1


def test_golden_a_second_tree_with_the_same_lock_builds_nothing_and_imports_its_own_commit(
    uv: Path, store: Path, tmp_path: Path
) -> None:
    first, second = (
        _tree(store, "omniclaude-aaaaaaaaaaaa"),
        _tree(store, "omniclaude-bbbbbbbbbbbb"),
    )
    handler = HandlerRemoteLaneSharedVenv()
    one = handler.handle(_request(first, store, uv))
    two = handler.handle(_request(second, store, uv))
    assert (one.built, two.built, two.linked) == (True, False, True)
    assert one.key == two.key
    assert _log(tmp_path).count("sync --frozen") == 1
    assert (
        _imports(second) == f"7 omniclaude-bbbbbbbbbbbb {second}/src/proj/__init__.py"
    )
    assert _imports(first).split()[1] == "omniclaude-aaaaaaaaaaaa"


def test_golden_an_ensured_tree_is_left_untouched_by_the_next_ensure(
    uv: Path, store: Path, tmp_path: Path
) -> None:
    tree = _tree(store, "omniclaude-aaaaaaaaaaaa")
    handler = HandlerRemoteLaneSharedVenv()
    handler.handle(_request(tree, store, uv))
    before = _snapshot(tree / ".venv")
    again = handler.handle(_request(tree, store, uv))
    assert (again.ok, again.built, again.linked) == (True, False, False)
    assert _snapshot(tree / ".venv") == before
    assert _log(tmp_path) == ["sync --frozen", "pip install"]


def test_a_venv_made_by_something_else_is_left_alone(uv: Path, store: Path) -> None:
    tree = _tree(store, "omniclaude-aaaaaaaaaaaa")
    (tree / ".venv" / "bin").mkdir(parents=True)
    (tree / ".venv" / "bin" / "python3").write_text("old")
    result = HandlerRemoteLaneSharedVenv().handle(_request(tree, store, uv))
    assert result.ok
    assert not result.linked
    assert (tree / ".venv" / "bin" / "python3").read_text() == "old"
    assert not (tree / ".venv" / MARKER).exists()


# ---- never rebuilt in place -----------------------------------------------------------------------


def test_a_new_lock_makes_a_new_venv_and_the_first_is_byte_identical(
    uv: Path, store: Path
) -> None:
    first = _tree(store, "omniclaude-aaaaaaaaaaaa")
    one = HandlerRemoteLaneSharedVenv().handle(_request(first, store, uv))
    before = _snapshot(store / "venvs" / one.key)
    time.sleep(0.02)
    second = _tree(store, "omniclaude-cccccccccccc", lock="lock two\n")
    two = HandlerRemoteLaneSharedVenv().handle(_request(second, store, uv))
    assert two.ok
    assert two.key != one.key
    assert {p.name for p in (store / "venvs").iterdir()} == {one.key, two.key}
    assert _snapshot(store / "venvs" / one.key) == before


def test_two_placements_at_once_build_one_venv_and_link_one_tree(
    uv: Path, store: Path, tmp_path: Path
) -> None:
    tree = _tree(store, "omniclaude-aaaaaaaaaaaa")
    results: list[object] = []

    def place() -> None:
        results.append(HandlerRemoteLaneSharedVenv().handle(_request(tree, store, uv)))

    threads = [threading.Thread(target=place) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r.ok for r in results)  # type: ignore[attr-defined]
    assert sum(r.built for r in results) == 1  # type: ignore[attr-defined]
    assert sum(r.linked for r in results) == 1  # type: ignore[attr-defined]
    assert _log(tmp_path).count("sync --frozen") == 1
    assert not list((store / "venvs").glob("*.building"))
    assert not list(tree.glob(".venv.building"))


# ---- error chain: a named injected break leaves no ready marker and no link ------------------------


def test_error_the_shared_build_fails_and_leaves_no_ready_marker_and_no_link(
    uv: Path, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _tree(store, "omniclaude-aaaaaaaaaaaa")
    monkeypatch.setenv("FAKE_UV_FAIL", "sync")
    result = HandlerRemoteLaneSharedVenv().handle(_request(tree, store, uv))
    assert (result.ok, result.refusal) == (False, "uv-failed")
    assert "injected break: sync" in result.detail
    assert not any(p.name != "." for p in (store / "venvs").iterdir())
    assert not (tree / ".venv").exists()
    monkeypatch.delenv("FAKE_UV_FAIL")
    assert HandlerRemoteLaneSharedVenv().handle(_request(tree, store, uv)).ok


def test_error_the_project_install_fails_and_leaves_no_link_but_keeps_the_ready_shared_venv(
    uv: Path, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _tree(store, "omniclaude-aaaaaaaaaaaa")
    monkeypatch.setenv("FAKE_UV_FAIL", "pip")
    result = HandlerRemoteLaneSharedVenv().handle(_request(tree, store, uv))
    assert (result.ok, result.refusal) == (False, "uv-failed")
    assert not (tree / ".venv").exists()
    assert (store / "venvs" / lock_key(tree / "uv.lock") / READY).is_file()
    monkeypatch.delenv("FAKE_UV_FAIL")
    again = HandlerRemoteLaneSharedVenv().handle(_request(tree, store, uv))
    assert again.ok
    assert again.linked
    assert not again.built


def test_error_a_tree_without_a_lock_is_refused_by_name(uv: Path, store: Path) -> None:
    tree = _tree(store, "omniclaude-aaaaaaaaaaaa")
    (tree / "uv.lock").unlink()
    result = HandlerRemoteLaneSharedVenv().handle(_request(tree, store, uv))
    assert (result.ok, result.refusal) == (False, "no-lock")


def test_error_uv_missing_is_refused_by_name(store: Path, tmp_path: Path) -> None:
    tree = _tree(store, "omniclaude-aaaaaaaaaaaa")
    result = HandlerRemoteLaneSharedVenv().handle(
        ModelRemoteLaneSharedVenvRequest(
            tree=str(tree), store_root=str(store), uv=str(tmp_path / "no-uv")
        )
    )
    assert (result.ok, result.refusal) == (False, "uv-missing")


def test_error_a_crashed_build_leaves_a_dead_lock_and_the_next_placement_takes_over(
    uv: Path, store: Path
) -> None:
    tree = _tree(store, "omniclaude-aaaaaaaaaaaa")
    key = lock_key(tree / "uv.lock")
    (store / "venvs").mkdir()
    crashed = store / "venvs" / key
    crashed.mkdir()
    (crashed / "half").write_text("x")
    lock = store / "venvs" / (key + ".building")
    lock.mkdir()
    holder = subprocess.Popen([sys.executable, "-c", "pass"])
    holder.wait()
    (lock / "pid").write_text(str(holder.pid))  # a process that is gone
    result = HandlerRemoteLaneSharedVenv().handle(_request(tree, store, uv))
    assert result.ok
    assert result.built
    assert not (store / "venvs" / key / "half").exists()


def test_error_request_models_refuse_unknown_and_empty_fields() -> None:
    with pytest.raises(ValidationError):
        ModelRemoteLaneSharedVenvRequest(tree="", store_root="x")
    with pytest.raises(ValidationError):
        ModelRemoteLaneSharedVenvRequest.model_validate(
            {"tree": "t", "store_root": "s", "extra": 1}
        )


# ---- prune ------------------------------------------------------------------------------------------


def _age(path: Path, days: float) -> None:
    then = time.time() - days * 86400
    os.utime(path, (then, then))


def test_prune_removes_an_unlinked_old_venv_and_keeps_a_linked_one_and_a_recent_one(
    uv: Path, store: Path
) -> None:
    linked_tree = _tree(store, "omniclaude-aaaaaaaaaaaa", lock="lock linked\n")
    linked = HandlerRemoteLaneSharedVenv().handle(_request(linked_tree, store, uv))
    old = HandlerRemoteLaneSharedVenv().handle(
        _request(_tree(store, "omniclaude-bbbbbbbbbbbb", lock="lock old\n"), store, uv)
    )
    recent = HandlerRemoteLaneSharedVenv().handle(
        _request(
            _tree(store, "omniclaude-cccccccccccc", lock="lock recent\n"), store, uv
        )
    )
    _remove_tree_link(store / "plugins" / "omniclaude-bbbbbbbbbbbb")
    _remove_tree_link(store / "plugins" / "omniclaude-cccccccccccc")
    _age(store / "venvs" / linked.key, 5)
    _age(store / "venvs" / old.key, 5)
    result = HandlerRemoteLaneSharedVenvPrune().handle(
        ModelRemoteLaneSharedVenvPruneRequest(store_root=str(store))
    )
    assert (result.pruned, result.linked, result.recent) == (1, 1, 1)
    assert sorted(p.name for p in (store / "venvs").iterdir()) == sorted(
        [linked.key, recent.key]
    )
    assert _imports(linked_tree).startswith(
        "7 "
    )  # the linked tree still imports through its shared venv


def _remove_tree_link(tree: Path) -> None:
    import shutil

    shutil.rmtree(tree / ".venv")


def test_prune_takes_at_most_the_cap_and_clears_a_dead_lock_and_a_half_removed_venv(
    uv: Path, store: Path
) -> None:
    keys = []
    for i in range(3):
        tree = _tree(store, f"omniclaude-{i:012d}", lock=f"lock {i}\n")
        keys.append(HandlerRemoteLaneSharedVenv().handle(_request(tree, store, uv)).key)
        _remove_tree_link(tree)
        _age(store / "venvs" / keys[-1], 3)
    half = store / "venvs" / ".pruning.dead.1"
    half.mkdir()
    (half / "f").write_text("x")
    result = HandlerRemoteLaneSharedVenvPrune().handle(
        ModelRemoteLaneSharedVenvPruneRequest(store_root=str(store), cap=2)
    )
    assert (result.pruned, result.deferred, result.leftovers) == (2, 1, 1)
    assert len(list((store / "venvs").iterdir())) == 1


def test_prune_of_a_store_with_no_venvs_is_empty(store: Path) -> None:
    result = HandlerRemoteLaneSharedVenvPrune().handle(
        ModelRemoteLaneSharedVenvPruneRequest(store_root=str(store))
    )
    assert result.model_dump() == {
        "pruned": 0,
        "linked": 0,
        "recent": 0,
        "deferred": 0,
        "leftovers": 0,
    }


# ---- the script the host runs ----------------------------------------------------------------------


def _run_script(
    script: str, store: Path, tree: Path, uv: Path, **env: str
) -> subprocess.CompletedProcess[str]:
    path = f"{uv.parent}:{os.environ['PATH']}"
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": path, "LR": str(store), "D": str(tree), **env},
        timeout=120,
    )


def _script() -> str:
    return shared_venv_script(
        "D", "LR", lock_file="uv.lock", venvs_dir="venvs", uv="uv", build_timeout_s=60
    )


def test_golden_the_script_prints_ready_and_the_tree_imports(
    uv: Path, store: Path
) -> None:
    tree = _tree(store, "omniclaude-aaaaaaaaaaaa")
    done = _run_script(_script(), store, tree, uv)
    assert done.returncode == 0, done.stderr
    ready = json.loads(done.stdout.split("PLUGIN-VENV-READY ", 1)[1])
    assert ready["built"]
    assert ready["linked"]
    assert ready["tree_venv_files"] < 30
    assert _imports(tree).startswith("7 omniclaude-aaaaaaaaaaaa")


def test_error_the_script_prints_a_typed_refusal_and_exits_21(
    uv: Path, store: Path
) -> None:
    tree = _tree(store, "omniclaude-aaaaaaaaaaaa")
    done = _run_script(_script(), store, tree, uv, FAKE_UV_FAIL="sync")
    assert done.returncode == 21
    assert done.stdout.startswith("PLUGIN-VENV-REFUSED uv-failed ")
    assert not (tree / ".venv").exists()


def test_the_prune_script_reports_on_stderr_and_takes_the_host_environment(
    uv: Path, store: Path
) -> None:
    tree = _tree(store, "omniclaude-aaaaaaaaaaaa")
    key = HandlerRemoteLaneSharedVenv().handle(_request(tree, store, uv)).key
    _remove_tree_link(tree)
    _age(store / "venvs" / key, 3)
    script = shared_venv_prune_script(
        "LR",
        plugins_dir="plugins",
        venvs_dir="venvs",
        retain_days_word='"${ONEX_PLUGIN_RETAIN_DAYS:-1}"',
        cap_word='"${ONEX_PLUGIN_PRUNE_MAX:-25}"',
        build_timeout_s=60,
    )
    kept = _run_script(script, store, tree, uv, ONEX_PLUGIN_RETAIN_DAYS="30")
    assert "PLUGIN-VENV-PRUNE-SUMMARY" in kept.stderr
    assert "pruned=0" in kept.stderr
    assert "recent=1" in kept.stderr
    gone = _run_script(script, store, tree, uv)
    assert "pruned=1" in gone.stderr
    assert "PLUGIN-VENV-PRUNED " + key in gone.stderr
    assert not (store / "venvs" / key).exists()


def test_a_removed_shared_venv_is_rebuilt_under_the_same_path_and_the_tree_works_again(
    uv: Path, store: Path
) -> None:
    import shutil

    tree = _tree(store, "omniclaude-aaaaaaaaaaaa")
    first = HandlerRemoteLaneSharedVenv().handle(_request(tree, store, uv))
    shutil.rmtree(store / "venvs" / first.key)
    again = HandlerRemoteLaneSharedVenv().handle(_request(tree, store, uv))
    assert again.ok
    assert again.built
    assert not again.linked  # the tree's links name the same path, which is whole again
    assert _imports(tree).startswith("7 omniclaude-aaaaaaaaaaaa")
