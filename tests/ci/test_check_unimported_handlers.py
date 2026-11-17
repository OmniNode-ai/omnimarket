# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the unimported-handler CI gate (OMN-10821)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ci.check_unimported_handlers import (
    find_dead_handlers,
    find_stale_baseline_entries,
    load_baseline,
)


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMMITTED_BASELINE = (
    _REPO_ROOT / "scripts" / "validation" / "unimported_handler_baseline.txt"
)


@pytest.mark.unit
def test_flags_handler_with_no_external_references(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "nodes/node_x/handlers/handler_x.py",
        "class HandlerXEffect:\n    pass\n",
    )
    dead = find_dead_handlers(tmp_path)
    assert len(dead) == 1
    assert "HandlerXEffect" in dead[0]


@pytest.mark.unit
def test_allows_handler_imported_in_init(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "nodes/node_x/handlers/handler_x.py",
        "class HandlerXEffect:\n    pass\n",
    )
    _write(
        tmp_path,
        "nodes/node_x/__init__.py",
        "from .handlers.handler_x import HandlerXEffect\n",
    )
    assert find_dead_handlers(tmp_path) == []


@pytest.mark.unit
def test_ignores_string_and_comment_mentions(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "nodes/node_x/handlers/handler_x.py",
        "class HandlerXEffect:\n    pass\n",
    )
    _write(
        tmp_path,
        "nodes/node_x/mentions.py",
        "# HandlerXEffect is not wired here\n"
        "DOC = 'HandlerXEffect is only mentioned as text'\n",
    )
    dead = find_dead_handlers(tmp_path)
    assert len(dead) == 1
    assert "HandlerXEffect" in dead[0]


@pytest.mark.unit
def test_allows_handler_referenced_in_registry(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "nodes/node_x/handlers/handler_x.py",
        "class HandlerXEffect:\n    pass\n",
    )
    _write(
        tmp_path,
        "nodes/node_x/registry/registry_x.py",
        "from omnimarket.nodes.node_x.handlers.handler_x import HandlerXEffect\n"
        "REGISTRY = {HandlerXEffect}\n",
    )
    assert find_dead_handlers(tmp_path) == []


@pytest.mark.unit
def test_contract_declaration_is_not_import_wiring(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "nodes/node_x/handlers/handler_x.py",
        "class HandlerXEffect:\n    pass\n",
    )
    _write(
        tmp_path,
        "nodes/node_x/contract.yaml",
        "handler:\n"
        "  module: omnimarket.nodes.node_x.handlers.handler_x\n"
        "  class: HandlerXEffect\n",
    )
    dead = find_dead_handlers(tmp_path)
    assert len(dead) == 1
    assert "HandlerXEffect" in dead[0]


@pytest.mark.unit
def test_allows_documented_legacy_baseline_entry(tmp_path: Path) -> None:
    handler = _write(
        tmp_path,
        "nodes/node_x/handlers/handler_x.py",
        "class HandlerXEffect:\n    pass\n",
    )
    repo_relative = handler.relative_to(tmp_path.parent.parent)
    baseline = _write(
        tmp_path,
        "scripts/validation/unimported_handler_baseline.txt",
        f"{repo_relative}:HandlerXEffect # documented legacy helper\n",
    )
    assert find_dead_handlers(tmp_path, baseline_path=baseline) == []


@pytest.mark.unit
def test_tracks_duplicate_handler_class_names_by_file(tmp_path: Path) -> None:
    first = _write(
        tmp_path,
        "nodes/node_x/handlers/handler_x.py",
        "class HandlerShared:\n    pass\n",
    )
    second = _write(
        tmp_path,
        "nodes/node_y/handlers/handler_y.py",
        "class HandlerShared:\n    pass\n",
    )
    baseline = _write(
        tmp_path,
        "scripts/validation/unimported_handler_baseline.txt",
        f"{first.relative_to(tmp_path.parent.parent)}:HandlerShared # legacy\n",
    )
    dead = find_dead_handlers(tmp_path, baseline_path=baseline)
    assert len(dead) == 1
    assert str(second.relative_to(tmp_path.parent.parent)) in dead[0]


@pytest.mark.unit
def test_skips_non_handler_classes(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "nodes/node_x/handlers/handler_x.py",
        "class ModelXConfig:\n    pass\n\nclass _InternalHelper:\n    pass\n",
    )
    assert find_dead_handlers(tmp_path) == []


@pytest.mark.unit
def test_respects_inline_allow_annotation(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "nodes/node_x/handlers/handler_x.py",
        "# unimported-handler-allow: abstract base, never directly imported\n"
        "class HandlerXBase:\n    pass\n",
    )
    assert find_dead_handlers(tmp_path) == []


@pytest.mark.unit
def test_skips_test_files(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "nodes/node_x/handlers/test_handler_x.py",
        "class HandlerXMock:\n    pass\n",
    )
    assert find_dead_handlers(tmp_path) == []


@pytest.mark.unit
def test_multiple_handlers_same_file(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "nodes/node_x/handlers/handler_x.py",
        "class HandlerXEffect:\n    pass\n\nclass HandlerXAlt:\n    pass\n",
    )
    dead = find_dead_handlers(tmp_path)
    names = {entry.split(": ")[-1] for entry in dead}
    assert "HandlerXEffect" in names
    assert "HandlerXAlt" in names


@pytest.mark.unit
def test_partial_import_clears_both_handlers(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "nodes/node_x/handlers/handler_x.py",
        "class HandlerXEffect:\n    pass\n\nclass HandlerXAlt:\n    pass\n",
    )
    _write(
        tmp_path,
        "nodes/node_x/__init__.py",
        "from .handlers.handler_x import HandlerXEffect, HandlerXAlt\n",
    )
    assert find_dead_handlers(tmp_path) == []


_HANDLER_REL = "src/omnimarket/nodes/node_x/handlers/handler_x.py"


def _baseline(tmp_path: Path, *entries: str) -> Path:
    return _write(tmp_path, "baseline.txt", "".join(f"{e} # legacy\n" for e in entries))


@pytest.mark.unit
def test_stale_baseline_entry_for_deleted_file_is_reported(tmp_path: Path) -> None:
    src = tmp_path / "src" / "omnimarket"
    _write(src, "nodes/node_x/__init__.py", "")
    baseline = _baseline(tmp_path, f"{_HANDLER_REL}:HandlerXEffect")
    assert find_stale_baseline_entries(src, baseline) == [
        f"{_HANDLER_REL}:HandlerXEffect"
    ]


@pytest.mark.unit
def test_stale_baseline_entry_for_now_imported_class_is_reported(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src" / "omnimarket"
    _write(
        src, "nodes/node_x/handlers/handler_x.py", "class HandlerXEffect:\n    pass\n"
    )
    _write(
        src,
        "nodes/node_x/__init__.py",
        "from .handlers.handler_x import HandlerXEffect\n",
    )
    baseline = _baseline(tmp_path, f"{_HANDLER_REL}:HandlerXEffect")
    assert find_stale_baseline_entries(src, baseline) == [
        f"{_HANDLER_REL}:HandlerXEffect"
    ]


@pytest.mark.unit
def test_live_baseline_entry_is_not_a_stale_baseline_entry(tmp_path: Path) -> None:
    src = tmp_path / "src" / "omnimarket"
    _write(
        src, "nodes/node_x/handlers/handler_x.py", "class HandlerXEffect:\n    pass\n"
    )
    baseline = _baseline(tmp_path, f"{_HANDLER_REL}:HandlerXEffect")
    assert find_stale_baseline_entries(src, baseline) == []
    assert find_dead_handlers(src, baseline_path=baseline) == []


@pytest.mark.unit
def test_stale_baseline_entry_when_class_gone_but_file_remains(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src" / "omnimarket"
    _write(
        src, "nodes/node_x/handlers/handler_x.py", "class HandlerXOther:\n    pass\n"
    )
    baseline = _baseline(tmp_path, f"{_HANDLER_REL}:HandlerXEffect")
    assert find_stale_baseline_entries(src, baseline) == [
        f"{_HANDLER_REL}:HandlerXEffect"
    ]


@pytest.mark.unit
def test_stale_baseline_entry_for_file_carrying_the_inline_allow_marker(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src" / "omnimarket"
    _write(
        src,
        "nodes/node_x/handlers/handler_x.py",
        "# unimported-handler-allow: reason\nclass HandlerXEffect:\n    pass\n",
    )
    baseline = _baseline(tmp_path, f"{_HANDLER_REL}:HandlerXEffect")
    assert find_stale_baseline_entries(src, baseline) == [
        f"{_HANDLER_REL}:HandlerXEffect"
    ]


_HELPER_REL = "src/omnimarket/handlers/unimported_handler_baseline.py"


def _tmp_repo(
    tmp_path: Path,
    baseline_entries: list[str],
    files: dict[str, str],
    *,
    with_helper: bool = True,
) -> Path:
    """Copy the real gate script and helper into a standalone repo under tmp."""
    copied = ["scripts/ci/check_unimported_handlers.py"]
    if with_helper:
        copied.append(_HELPER_REL)
    for rel in copied:
        destination = tmp_path / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(_REPO_ROOT / rel, destination)
    _write(
        tmp_path,
        "scripts/validation/unimported_handler_baseline.txt",
        "".join(f"{entry} # legacy\n" for entry in baseline_entries),
    )
    for rel, body in files.items():
        _write(tmp_path, f"src/omnimarket/{rel}", body)
    return tmp_path


def _run_gate(repo: Path) -> subprocess.CompletedProcess[str]:
    """Run the copied gate's main() in isolated mode from its own repo root."""
    script = repo / "scripts" / "ci" / "check_unimported_handlers.py"
    return subprocess.run(
        [sys.executable, "-I", str(script)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.unit
def test_main_exits_1_and_names_stale_baseline_entry(tmp_path: Path) -> None:
    repo = _tmp_repo(
        tmp_path,
        [f"{_HANDLER_REL}:HandlerXEffect"],
        {"nodes/node_x/__init__.py": ""},
    )
    result = _run_gate(repo)
    assert result.returncode == 1
    assert f"{_HANDLER_REL}:HandlerXEffect" in result.stdout


@pytest.mark.unit
def test_main_exits_0_when_no_stale_baseline_entry_exists(tmp_path: Path) -> None:
    repo = _tmp_repo(
        tmp_path,
        [f"{_HANDLER_REL}:HandlerXEffect"],
        {
            "nodes/node_x/handlers/handler_x.py": "class HandlerXEffect:\n    pass\n",
        },
    )
    result = _run_gate(repo)
    assert result.returncode == 0
    assert "OK" in result.stdout


@pytest.mark.unit
def test_main_reports_stale_and_dead_handlers_from_one_run(tmp_path: Path) -> None:
    stale_entry = f"{_HANDLER_REL}:HandlerXEffect"
    repo = _tmp_repo(
        tmp_path,
        [stale_entry],
        {"nodes/node_y/handlers/handler_y.py": "class HandlerYEffect:\n    pass\n"},
    )
    result = _run_gate(repo)
    assert result.returncode == 1
    assert stale_entry in result.stdout
    assert "HandlerYEffect" in result.stdout


@pytest.mark.unit
def test_script_loads_without_importing_omnimarket_or_pydantic() -> None:
    code = "\n".join(
        [
            "import importlib.util, sys",
            "spec = importlib.util.spec_from_file_location(",
            "    'gate', sys.argv[1]",
            ")",
            "module = importlib.util.module_from_spec(spec)",
            "spec.loader.exec_module(module)",
            "print(repr(('omnimarket' in sys.modules, 'pydantic' in sys.modules)))",
        ]
    )
    script = _REPO_ROOT / "scripts" / "ci" / "check_unimported_handlers.py"
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "(False, False)"


@pytest.mark.unit
def test_main_exits_2_with_a_message_when_the_scan_helper_is_missing(
    tmp_path: Path,
) -> None:
    """A checkout without the helper is an invocation error, never a traceback."""
    repo = _tmp_repo(tmp_path, [], {"nodes/node_x/__init__.py": ""}, with_helper=False)
    assert not (repo / _HELPER_REL).exists()
    result = _run_gate(repo)
    assert result.returncode == 2
    assert "unimported_handler_baseline.py" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.unit
@pytest.mark.parametrize("flags", [[], ["--transport-imports"]])
def test_gate_exits_2_with_a_message_when_the_helper_lacks_a_name(
    tmp_path: Path, flags: list[str]
) -> None:
    """A helper that loads but lacks a name the gate binds is an invocation error."""
    repo = _tmp_repo(tmp_path, [], {"nodes/node_x/__init__.py": ""})
    helper = repo / _HELPER_REL
    helper.write_text(
        helper.read_text(encoding="utf-8").replace(
            "def scan_unimported_handlers", "def scan_renamed_handlers"
        ),
        encoding="utf-8",
    )
    script = repo / "scripts" / "ci" / "check_unimported_handlers.py"
    result = subprocess.run(
        [sys.executable, "-I", str(script), *flags],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2, result.stdout
    assert "scan_unimported_handlers" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.unit
def test_transport_imports_exits_2_with_a_message_when_the_scan_helper_is_missing(
    tmp_path: Path,
) -> None:
    repo = _tmp_repo(tmp_path, [], {"nodes/node_x/__init__.py": ""}, with_helper=False)
    script = repo / "scripts" / "ci" / "check_unimported_handlers.py"
    result = subprocess.run(
        [sys.executable, "-I", str(script), "--transport-imports"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


@pytest.mark.unit
def test_script_registers_the_scan_helper_under_a_private_name() -> None:
    code = "\n".join(
        [
            "import importlib.util, sys",
            "spec = importlib.util.spec_from_file_location('gate', sys.argv[1])",
            "module = importlib.util.module_from_spec(spec)",
            "spec.loader.exec_module(module)",
            "print('unimported_handler_baseline' in sys.modules,",
            "      '_omnimarket_unimported_handler_scan' in sys.modules)",
        ]
    )
    script = _REPO_ROOT / "scripts" / "ci" / "check_unimported_handlers.py"
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    # Registered under the private name only: the public name stays unclaimed, and
    # the private name is present (a script that registered nothing would print False False).
    assert result.stdout.strip() == "False True"


@pytest.mark.unit
def test_transport_imports_print_paths_relative_to_the_repo_root(
    tmp_path: Path,
) -> None:
    """Reaches the repo_relative call in transport_import_main (output path form)."""
    violating = "node_x/handlers/handler_x.py"
    repo = _tmp_repo(
        tmp_path,
        [],
        {
            f"nodes/{violating}": (
                "import importlib\n"
                "def load():\n"
                "    return importlib.import_module('omnimarket.nodes.node_y.mod')\n"
            )
        },
    )
    script = repo / "scripts" / "ci" / "check_unimported_handlers.py"
    target = repo / "src" / "omnimarket" / "nodes" / violating
    result = subprocess.run(
        [sys.executable, "-I", str(script), "--transport-imports", str(target)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert (
        f"src/omnimarket/nodes/{violating}:3: dynamic transport/node import"
        in result.stdout
    )
    assert str(repo) not in result.stdout


@pytest.mark.unit
def test_committed_baseline_has_no_stale_baseline_entry() -> None:
    assert _COMMITTED_BASELINE.is_file(), f"missing baseline: {_COMMITTED_BASELINE}"
    stale = find_stale_baseline_entries(
        _REPO_ROOT / "src" / "omnimarket",
        _COMMITTED_BASELINE,
    )
    assert stale == []


# The baseline only shrinks, so this count only goes down. An entry that was
# pruned because a coincidental name reference made its class look imported
# cannot come back: if that class later has no real import again, the gate
# reports it as a dead handler and this count refuses a re-added line. The way
# back is to wire the handler (import it where it is used) or to mark its file
# with an inline ``# unimported-handler-allow: <reason>`` annotation, never to
# grow the baseline.
_BASELINE_COUNT = 61


@pytest.mark.unit
def test_baseline_entry_count_is_the_exact_baseline_count() -> None:
    assert _COMMITTED_BASELINE.is_file(), f"missing baseline: {_COMMITTED_BASELINE}"
    count = len(load_baseline(_COMMITTED_BASELINE))
    assert count == _BASELINE_COUNT, (
        f"unimported handler baseline holds {count} entries, expected exactly "
        f"{_BASELINE_COUNT}; shrink the baseline and lower _BASELINE_COUNT in "
        "the same change, never grow it"
    )


@pytest.mark.unit
def test_selector_node_handlers_have_wiring_evidence() -> None:
    assert _COMMITTED_BASELINE.is_file(), f"missing baseline: {_COMMITTED_BASELINE}"
    dead = find_dead_handlers(
        _REPO_ROOT / "src" / "omnimarket",
        baseline_path=_COMMITTED_BASELINE,
    )
    selector_entries = [
        entry for entry in dead if "node_test_selection_selector_compute" in entry
    ]
    assert selector_entries == []


@pytest.mark.unit
def test_every_caller_runs_the_gate_script_in_place_from_a_full_checkout() -> None:
    """The callers named by the helper's docstring run the script where it sits.

    The two pre-commit hooks and the two CI lint steps run the script by its repo
    path, so the helper under src/omnimarket/handlers is present in each of them;
    no caller copies the script elsewhere.
    """
    gate = "scripts/ci/check_unimported_handlers.py"
    precommit = (_REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    workflow = (_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    for text in (precommit, workflow):
        assert f"uv run python {gate} --transport-imports" in text
        assert f"uv run python {gate}\n" in text
    assert (_REPO_ROOT / gate).is_file()
    assert (_REPO_ROOT / _HELPER_REL).is_file()
