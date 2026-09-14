# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for scripts/validation/check_vendor_token_scope.py (OMN-18374).

The gate's whole claim is that a vendor's name is refused everywhere in
``src/omnimarket/`` except inside an effect node's ``adapters/`` package, and
that the recorded backlog can only shrink. Each of those halves gets a control
here rather than a single happy path:

* positive control -- a token OUTSIDE ``adapters/`` fails;
* negative control -- the same token UNDER ``adapters/`` passes, which is what
  proves the failure above came from placement and not from the token merely
  being present somewhere in the tree;
* ratchet -- a shrink passes, a growth fails, and a growing regeneration is
  refused;
* exactness -- regenerating the COMMITTED baseline against the REAL tree is
  byte-identical, which is the ticket's AC3 falsifier.

The fixture trees are synthetic and built under ``tmp_path``; the two tests that
touch the real tree say so in their names.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "validation" / "check_vendor_token_scope.py"

# The fixture trees below spell the deny-listed token deliberately: a scanner
# test that cannot write the string it scans for cannot test anything. These
# files live under tests/, which the gate never scans.
_TOKEN = "repowise"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "check_vendor_token_scope", _SCRIPT_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_vendor_token_scope"] = module
    spec.loader.exec_module(module)
    return module


MODULE = _load_module()


def _denylist(root: Path, *tokens: str) -> Path:
    path = root / "vendor_tokens.yaml"
    path.write_text(
        "tokens:\n"
        + "".join(
            f"  - token: {token}\n    ticket: OMN-18374\n    reason: test\n"
            for token in tokens
        )
    )
    return path


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _run(
    tmp_path: Path,
    baseline: Path,
    argv: list[str] | None = None,
    tokens: tuple[str, ...] = (_TOKEN,),
) -> int:
    return int(
        MODULE.main(
            [
                "--scan-root",
                str(tmp_path / "src" / "omnimarket"),
                "--repo-root",
                str(tmp_path),
                "--denylist",
                str(_denylist(tmp_path, *tokens)),
                "--baseline",
                str(baseline),
                "--min-files",
                "1",
                *(argv or []),
            ]
        )
    )


# ---------------------------------------------------------------------------
# Positive / negative controls
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_token_outside_adapters_fails(tmp_path: Path) -> None:
    """Positive control: a vendor token in a handler is refused."""
    _write(
        tmp_path,
        "src/omnimarket/nodes/node_thing_effect/handlers/handler_thing.py",
        f'CLIENT = "{_TOKEN}"\n',
    )
    assert _run(tmp_path, tmp_path / "baseline.txt") == 1


@pytest.mark.unit
def test_token_under_adapters_passes(tmp_path: Path) -> None:
    """Negative control: the SAME token under adapters/ is the sanctioned home.

    Paired with the test above, this is what proves the gate keys on PLACEMENT
    rather than on the token being present anywhere in the tree.
    """
    _write(
        tmp_path,
        f"src/omnimarket/nodes/node_thing_effect/adapters/adapter_{_TOKEN}_cli.py",
        f'CLIENT = "{_TOKEN}"\n',
    )
    assert _run(tmp_path, tmp_path / "baseline.txt") == 0


@pytest.mark.unit
def test_adapters_exemption_does_not_span_other_packages(tmp_path: Path) -> None:
    """`adapters/` earns the exemption only directly under a node package.

    A top-level `src/omnimarket/adapters/` tree, or a nested
    `handlers/adapters/`, is not an effect node's adapter boundary, and reading
    the exemption as "any path containing adapters" would silently license both.
    """
    _write(tmp_path, "src/omnimarket/adapters/legacy.py", f'X = "{_TOKEN}"\n')
    _write(
        tmp_path,
        "src/omnimarket/nodes/node_thing_effect/handlers/adapters/inner.py",
        f'X = "{_TOKEN}"\n',
    )
    assert _run(tmp_path, tmp_path / "baseline.txt") == 1


@pytest.mark.unit
def test_match_is_case_insensitive(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/omnimarket/nodes/node_thing_effect/contract.yaml",
        "provider: RepoWise\n",
    )
    assert _run(tmp_path, tmp_path / "baseline.txt") == 1


@pytest.mark.unit
def test_path_alone_is_a_violation(tmp_path: Path) -> None:
    """A package NAMED after a vendor leaks it even with clean file contents."""
    _write(
        tmp_path,
        f"src/omnimarket/nodes/node_kb_{_TOKEN}_index_effect/__init__.py",
        "# nothing to see\n",
    )
    assert _run(tmp_path, tmp_path / "baseline.txt") == 1


@pytest.mark.unit
def test_yaml_values_and_identifiers_are_both_scanned(tmp_path: Path) -> None:
    contract = _write(
        tmp_path,
        "src/omnimarket/nodes/node_thing_effect/contract.yaml",
        f"backend: {_TOKEN}\n",
    )
    identifier = _write(
        tmp_path,
        "src/omnimarket/nodes/node_thing_effect/models/model_thing.py",
        f"{_TOKEN}_enabled: bool\n",
    )
    rows, _ = MODULE.scan(tmp_path / "src" / "omnimarket", (_TOKEN,), tmp_path)
    found = {row.path for row in rows}
    assert str(contract.relative_to(tmp_path)) in found
    assert str(identifier.relative_to(tmp_path)) in found


# ---------------------------------------------------------------------------
# Ratchet semantics
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_baselined_violation_passes(tmp_path: Path) -> None:
    relative = "src/omnimarket/nodes/node_thing_effect/handlers/handler_thing.py"
    _write(tmp_path, relative, f'CLIENT = "{_TOKEN}"\n')
    baseline = tmp_path / "baseline.txt"
    baseline.write_text(f"# header\n{relative}::{_TOKEN} 1\n")
    assert _run(tmp_path, baseline) == 0


@pytest.mark.unit
def test_baseline_shrink_passes(tmp_path: Path) -> None:
    """Removing occurrences is the ratchet working, never a failure."""
    relative = "src/omnimarket/nodes/node_thing_effect/handlers/handler_thing.py"
    _write(tmp_path, relative, f'CLIENT = "{_TOKEN}"\n')
    baseline = tmp_path / "baseline.txt"
    baseline.write_text(f"# header\n{relative}::{_TOKEN} 4\n")
    assert _run(tmp_path, baseline) == 0


@pytest.mark.unit
def test_baseline_row_fully_removed_passes(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/omnimarket/nodes/node_thing_effect/handlers/handler_thing.py",
        "CLIENT = None\n",
    )
    baseline = tmp_path / "baseline.txt"
    baseline.write_text(
        f"# header\nsrc/omnimarket/nodes/node_thing_effect/handlers/handler_thing.py::{_TOKEN} 2\n"
    )
    assert _run(tmp_path, baseline) == 0


@pytest.mark.unit
def test_baseline_growth_fails(tmp_path: Path) -> None:
    """A baselined file that gains occurrences is a NEW leak, not a tolerated one."""
    relative = "src/omnimarket/nodes/node_thing_effect/handlers/handler_thing.py"
    _write(tmp_path, relative, f'A = "{_TOKEN}"\nB = "{_TOKEN}"\nC = "{_TOKEN}"\n')
    baseline = tmp_path / "baseline.txt"
    baseline.write_text(f"# header\n{relative}::{_TOKEN} 1\n")
    assert _run(tmp_path, baseline) == 1


@pytest.mark.unit
def test_new_file_fails_even_when_other_rows_are_baselined(tmp_path: Path) -> None:
    baselined = "src/omnimarket/nodes/node_thing_effect/handlers/handler_thing.py"
    _write(tmp_path, baselined, f'A = "{_TOKEN}"\n')
    _write(
        tmp_path,
        "src/omnimarket/nodes/node_other_effect/handlers/handler_other.py",
        f'A = "{_TOKEN}"\n',
    )
    baseline = tmp_path / "baseline.txt"
    baseline.write_text(f"# header\n{baselined}::{_TOKEN} 1\n")
    assert _run(tmp_path, baseline) == 1


# ---------------------------------------------------------------------------
# Baseline regeneration
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_write_baseline_refuses_growth(tmp_path: Path) -> None:
    """ "Fix the baseline" may not become the way a new leak lands."""
    relative = "src/omnimarket/nodes/node_thing_effect/handlers/handler_thing.py"
    _write(tmp_path, relative, f'A = "{_TOKEN}"\nB = "{_TOKEN}"\n')
    baseline = tmp_path / "baseline.txt"
    baseline.write_text(f"# header\n{relative}::{_TOKEN} 1\n")
    before = baseline.read_text()

    assert _run(tmp_path, baseline, ["--write-baseline"]) == 1
    assert baseline.read_text() == before

    assert _run(tmp_path, baseline, ["--write-baseline", "--allow-growth"]) == 0
    assert baseline.read_text() != before


@pytest.mark.unit
def test_write_baseline_is_idempotent(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/omnimarket/nodes/node_thing_effect/handlers/handler_thing.py",
        f'A = "{_TOKEN}"\n',
    )
    baseline = tmp_path / "baseline.txt"
    assert _run(tmp_path, baseline, ["--write-baseline"]) == 0
    first = baseline.read_bytes()
    assert _run(tmp_path, baseline, ["--write-baseline"]) == 0
    assert baseline.read_bytes() == first


# ---------------------------------------------------------------------------
# Fail-closed behaviour
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_vacuity_guard_fails_on_a_collapsed_scan(tmp_path: Path) -> None:
    """An empty result is not evidence of absence; a tiny scan is a broken scan."""
    _write(tmp_path, "src/omnimarket/nodes/node_thing_effect/__init__.py", "\n")
    baseline = tmp_path / "baseline.txt"
    assert (
        MODULE.main(
            [
                "--scan-root",
                str(tmp_path / "src" / "omnimarket"),
                "--repo-root",
                str(tmp_path),
                "--denylist",
                str(_denylist(tmp_path, _TOKEN)),
                "--baseline",
                str(baseline),
                "--min-files",
                "500",
            ]
        )
        == 1
    )


@pytest.mark.unit
def test_missing_denylist_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        MODULE.load_tokens(tmp_path / "absent.yaml")


@pytest.mark.unit
def test_empty_denylist_raises(tmp_path: Path) -> None:
    path = tmp_path / "vendor_tokens.yaml"
    path.write_text("tokens: []\n")
    with pytest.raises(ValueError, match="declares no tokens"):
        MODULE.load_tokens(path)


@pytest.mark.unit
def test_malformed_baseline_row_raises(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("src/omnimarket/nodes/n/f.py::repowise\n")
    with pytest.raises(ValueError, match="malformed baseline row"):
        MODULE.read_baseline(baseline)


# ---------------------------------------------------------------------------
# The real tree
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_committed_denylist_declares_the_repowise_token() -> None:
    tokens = MODULE.load_tokens(MODULE.DENYLIST)
    assert _TOKEN in tokens


@pytest.mark.unit
def test_committed_baseline_matches_the_real_tree_exactly() -> None:
    """AC3: regenerating the baseline is byte-identical to what is committed."""
    tokens = MODULE.load_tokens(MODULE.DENYLIST)
    rows, files = MODULE.scan(MODULE.SCAN_ROOT, tokens, MODULE.REPO_ROOT)
    assert files >= MODULE.DEFAULT_MIN_EXPECTED_FILES
    expected = MODULE._HEADER + "".join(f"{row.render()}\n" for row in rows)
    assert MODULE.BASELINE.read_text() == expected


@pytest.mark.unit
def test_real_tree_has_no_unbaselined_leak() -> None:
    assert MODULE.main([]) == 0
