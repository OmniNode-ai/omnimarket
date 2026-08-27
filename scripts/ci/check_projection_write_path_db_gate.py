#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15909 -- path-triggered real-DB proof gate for projection write-path diffs.

The escape this gate closes: ``DelegationProjectionRunner`` (OMN-15905's
ported async writer) bound a wall-clock ``.isoformat()`` STRING to a
``TIMESTAMPTZ`` param at four call sites. The seam test guarding that class of
change (``tests/test_omn15905_delegation_projection_writer_seam.py``) drove
the write path against an ``AsyncMock`` DB double, which accepts a ``str`` just
as happily as a real ``datetime.datetime`` -- only live asyncpg enforces the
column type and raises ``asyncpg.exceptions.DataError``. The mock-DB blind
spot let the defect merge, deploy, and CrashLoopBackOff in production before
anyone with a real Postgres connection ever exercised the path.

This gate is baseline-friendly and enforcement-only (Operating Rule #5): it
does not grade test QUALITY, it enforces PRESENCE. When a diff touches a
projection write-path surface --

  * ``src/omnimarket/nodes/node_projection_*/handlers/**`` (any
    ``ProjectionRunner``/handler module that owns a DB write path), or
  * ``src/omnimarket/projection/runner.py`` (the shared ``BaseProjectionRunner``
    every projection write path inherits from)

-- the SAME diff must also touch at least one test file under ``tests/`` that
carries BOTH the ``@pytest.mark.integration`` marker AND the
``INTEGRATION_POSTGRES`` real-Postgres-DSN signal already established as this
repo's real-DB-proof idiom (``tests/test_writer_tenant_isolation_omn14898.py``,
``tests/test_rls_tranche2_omn14894.py``, and this ticket's own
``tests/test_omn15909_real_postgres_projection_write_path_gate.py`` all use
it). A diff with only mock-DB coverage of a write-path change fails closed.

Usage
-----
    check_projection_write_path_db_gate.py --changed-ref GIT_REF
    check_projection_write_path_db_gate.py --staged
    check_projection_write_path_db_gate.py --selftest

``--selftest`` proves the gate's own RED/GREEN behavior against synthetic
changed-file lists + a temp tree (no git state, no DB required) -- what the
pre-commit hook and this module's own unit test run locally.

Exit codes: 0 = gate passed; 1 = a write-path diff lacks integration coverage;
2 = usage/input error (fail-closed).

SYNC: ci.yml job `lint`, step "Projection write-path real-DB gate (OMN-15909)"
      + pre-commit hook 'projection-write-path-db-gate'.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

NODES_DIR_PARTS: tuple[str, str, str] = ("src", "omnimarket", "nodes")
RUNNER_FILE = Path("src/omnimarket/projection/runner.py")
TESTS_DIR_NAME = "tests"

INTEGRATION_MARKER = "@pytest.mark.integration"
REAL_DB_SIGNAL = "INTEGRATION_POSTGRES"


def _run_git(args: list[str]) -> list[str]:
    proc = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        print(
            "projection-write-path-db-gate: git diff failed "
            f"(exit {proc.returncode}): {proc.stderr.strip()}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _changed_files_from_ref(ref: str) -> list[str]:
    return _run_git(["diff", "--name-only", f"{ref}...HEAD"])


def _changed_files_from_staged() -> list[str]:
    return _run_git(["diff", "--cached", "--name-only"])


def is_write_path_target(raw_path: str) -> bool:
    """True when ``raw_path`` is a projection write-path surface this gate covers."""
    path = Path(raw_path)
    if path == RUNNER_FILE:
        return True
    parts = path.parts
    if (
        len(parts) >= 5
        and parts[0] == NODES_DIR_PARTS[0]
        and parts[1] == NODES_DIR_PARTS[1]
        and parts[2] == NODES_DIR_PARTS[2]
        and parts[3].startswith("node_projection_")
        and parts[4] == "handlers"
        and path.suffix == ".py"
    ):
        return True
    return False


def _file_has_real_db_integration_signal(full_path: Path) -> bool:
    if not full_path.is_file():
        # A deleted/renamed-away file cannot satisfy the requirement.
        return False
    try:
        content = full_path.read_text(errors="replace")
    except OSError:
        return False
    return INTEGRATION_MARKER in content and REAL_DB_SIGNAL in content


def is_real_db_integration_test_change(raw_path: str, repo_root: Path) -> bool:
    path = Path(raw_path)
    if not path.parts or path.parts[0] != TESTS_DIR_NAME:
        return False
    if path.suffix != ".py":
        return False
    return _file_has_real_db_integration_signal(repo_root / path)


@dataclass
class GateResult:
    write_path_targets: list[str] = field(default_factory=list)
    covering_tests: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.write_path_targets or bool(self.covering_tests)


def evaluate(changed_files: list[str], repo_root: Path) -> GateResult:
    targets = sorted({f for f in changed_files if is_write_path_target(f)})
    if not targets:
        return GateResult()
    covering = sorted(
        {f for f in changed_files if is_real_db_integration_test_change(f, repo_root)}
    )
    return GateResult(write_path_targets=targets, covering_tests=covering)


def run(*, changed_files: list[str], repo_root: Path, output_json: bool) -> int:
    result = evaluate(changed_files, repo_root)

    if output_json:
        print(
            json.dumps(
                {
                    "status": "ok" if result.passed else "fail",
                    "write_path_targets": result.write_path_targets,
                    "covering_tests": result.covering_tests,
                },
                indent=2,
            )
        )
        return 0 if result.passed else 1

    if not result.write_path_targets:
        print(
            "projection-write-path-db-gate: no changed projection write-path "
            "files - PASS"
        )
        return 0

    print(
        "projection-write-path-db-gate: "
        f"{len(result.write_path_targets)} write-path file(s) changed:"
    )
    for target in result.write_path_targets:
        print(f"  - {target}")

    if result.covering_tests:
        print(
            f"projection-write-path-db-gate: PASS -- "
            f"{len(result.covering_tests)} real-DB integration test change(s) "
            "found:"
        )
        for test in result.covering_tests:
            print(f"  - {test}")
        return 0

    print(
        "\n::error::projection-write-path-db-gate: FAIL -- this diff touches a "
        "projection write-path file but includes no accompanying real-Postgres "
        "integration test change.\n"
        f"  A test file under {TESTS_DIR_NAME}/ must carry BOTH "
        f"'{INTEGRATION_MARKER}' AND the '{REAL_DB_SIGNAL}' real-DSN signal "
        "(the established idiom -- see "
        "tests/test_writer_tenant_isolation_omn14898.py or "
        "tests/test_omn15909_real_postgres_projection_write_path_gate.py).\n"
        "  A mock-DB-only test cannot catch a str-vs-datetime column-type "
        "mismatch (OMN-15905) -- only a real Postgres connection enforces "
        "column types."
    )
    return 1


def selftest() -> int:
    ok = True
    with tempfile.TemporaryDirectory() as td:
        repo_root = Path(td)
        write_path_file = (
            "src/omnimarket/nodes/node_projection_delegation/handlers/"
            "handler_delegation.py"
        )
        runner_file = str(RUNNER_FILE)
        unrelated_file = "src/omnimarket/nodes/node_other/handlers/handler_x.py"

        covering_test_rel = "tests/test_fake_real_db_gate_selftest.py"
        covering_test_path = repo_root / covering_test_rel
        covering_test_path.parent.mkdir(parents=True, exist_ok=True)
        covering_test_path.write_text(
            f"import os\n\n# {INTEGRATION_MARKER}\nasync def test_x():\n"
            f"    os.environ.get('{REAL_DB_SIGNAL}_HOST')\n",
            encoding="utf-8",
        )

        noncovering_test_rel = "tests/test_fake_mock_only_selftest.py"
        noncovering_test_path = repo_root / noncovering_test_rel
        noncovering_test_path.write_text(
            "from unittest.mock import AsyncMock\n\ndef test_x():\n    AsyncMock()\n",
            encoding="utf-8",
        )

        # Case (a): no write-path files touched -> PASS regardless of tests.
        r = evaluate([unrelated_file], repo_root)
        if not r.passed:
            ok = False
            print("SELFTEST FAIL (case a should pass): unrelated-only diff")
        else:
            print("SELFTEST ok: case (a) no write-path change -> PASS")

        # Case (b) RED: write-path file changed, only a mock-only test changed.
        r = evaluate([write_path_file, noncovering_test_rel], repo_root)
        if r.passed:
            ok = False
            print(
                "SELFTEST FAIL (case b should be RED): mock-only test satisfied "
                "the gate"
            )
        else:
            print("SELFTEST ok: case (b) write-path + mock-only test -> RED")

        # Case (c) RED: write-path file changed, no test changed at all.
        r = evaluate([write_path_file], repo_root)
        if r.passed:
            ok = False
            print("SELFTEST FAIL (case c should be RED): no test change at all")
        else:
            print("SELFTEST ok: case (c) write-path change, no test -> RED")

        # Case (d) GREEN: write-path file changed, a real integration test
        # (marker + INTEGRATION_POSTGRES signal) changed in the same diff.
        r = evaluate([write_path_file, covering_test_rel], repo_root)
        if not r.passed:
            ok = False
            print(
                "SELFTEST FAIL (case d should be GREEN): covering integration "
                "test not recognised"
            )
        else:
            print("SELFTEST ok: case (d) write-path + real-DB integration test -> PASS")

        # Case (e) GREEN: the shared runner.py changed with covering test.
        r = evaluate([runner_file, covering_test_rel], repo_root)
        if not r.passed:
            ok = False
            print("SELFTEST FAIL (case e should be GREEN): runner.py path not matched")
        else:
            print("SELFTEST ok: case (e) shared runner.py + covering test -> PASS")

        # Case (f) RED: a deleted covering test cannot satisfy the requirement
        # (file no longer exists at repo_root on disk).
        r = evaluate([write_path_file, "tests/test_deleted_selftest.py"], repo_root)
        if r.passed:
            ok = False
            print(
                "SELFTEST FAIL (case f should be RED): nonexistent file satisfied gate"
            )
        else:
            print("SELFTEST ok: case (f) nonexistent/deleted test file -> RED")

    print("SELFTEST PASSED" if ok else "SELFTEST FAILED")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--changed-ref",
        metavar="GIT_REF",
        help="validate the diff since GIT_REF",
    )
    mode.add_argument(
        "--staged",
        action="store_true",
        help="validate files staged for commit",
    )
    mode.add_argument("--selftest", action="store_true")
    parser.add_argument("--json", action="store_true", dest="output_json")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    changed = (
        _changed_files_from_staged()
        if args.staged
        else _changed_files_from_ref(args.changed_ref)
    )
    return run(
        changed_files=changed, repo_root=Path.cwd(), output_json=args.output_json
    )


if __name__ == "__main__":
    raise SystemExit(main())
