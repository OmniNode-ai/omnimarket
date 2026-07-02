# onex-allow-internal-ip: no LAN IPs here; this header exists only to match
# the sibling test_aislop_hardcoded_config.py convention.
"""Tests for the hardcoded-paths check in NodeAislopSweep (OMN-13860).

CLAUDE.md rule #6: any string starting with /Users/ or /Volumes/ in source
code is a cross-machine portability bug. This check mirrors the ARCH-005
invariant already enforced by node_architectural_invariant_loop, closing the
gap where aislop_sweep's own SKILL.md documented a `hardcoded-paths` check
category that the handler never actually implemented.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_aislop_sweep.handlers.handler_aislop_sweep import (
    AislopSweepRequest,
    NodeAislopSweep,
)


def _run_check(py_content: str, filename: str = "handler.py") -> list:
    handler = NodeAislopSweep(event_bus=EventBusInmemory())
    with tempfile.TemporaryDirectory() as tmpdir:
        src = Path(tmpdir) / "src"
        src.mkdir()
        (src / filename).write_text(py_content, encoding="utf-8")
        result = handler.handle(
            AislopSweepRequest(
                target_dirs=[tmpdir],
                checks=["hardcoded-paths"],
            )
        )
    return result.findings


class TestHardcodedPathDetection:
    def test_detects_users_path(self) -> None:
        # test-literal-ok: planted canary, this is the pattern under test
        findings = _run_check('ROOT = "/Users/alice/Code/project"\n')
        assert len(findings) == 1
        assert findings[0].check == "hardcoded-paths"
        assert findings[0].severity == "ERROR"
        assert findings[0].confidence == "HIGH"
        assert "/Users/alice/Code/project" in findings[0].message

    def test_detects_volumes_path(self) -> None:
        # test-literal-ok: planted canary, this is the pattern under test
        findings = _run_check(
            'WORKTREES_ROOT = "/Volumes/PRO-G40/Code/omni_worktrees"\n'
        )
        assert len(findings) == 1
        assert findings[0].check == "hardcoded-paths"

    def test_detects_path_object_literal(self) -> None:
        # test-literal-ok: planted canary, this is the pattern under test
        findings = _run_check('p = Path("/Users/bob/repo/file.txt")\n')
        assert len(findings) == 1


class TestHardcodedPathAllowlist:
    def test_local_path_ok_annotation_suppresses_finding(self) -> None:
        findings = _run_check('ROOT = "/Users/alice/Code"  # local-path-ok\n')
        assert findings == []

    def test_onex_allow_internal_ip_annotation_suppresses_finding(self) -> None:
        findings = _run_check(
            'ROOT = "/Users/alice/Code"  # onex-allow-internal-ip: fixture\n'
        )
        assert findings == []

    def test_noqa_annotation_suppresses_finding(self) -> None:
        findings = _run_check('ROOT = "/Users/alice/Code"  # noqa\n')
        assert findings == []

    def test_annotation_on_other_line_does_not_suppress(self) -> None:
        # local-path-ok
        findings = _run_check(
            '# local-path-ok\nROOT = "/Users/alice/Code"\n', filename="handler2.py"
        )
        assert len(findings) == 1


class TestHardcodedPathExclusions:
    def test_clean_file_produces_no_findings(self) -> None:
        findings = _run_check('ROOT = os.environ["OMNI_HOME"]\n')
        assert findings == []

    def test_relative_path_not_flagged(self) -> None:
        findings = _run_check('ROOT = "./omni_worktrees"\n')
        assert findings == []

    def test_hardcoded_paths_registered_in_all_checks(self) -> None:
        """Verify the check is included in ALL_CHECKS (wired into default scan)."""
        assert "hardcoded-paths" in NodeAislopSweep.ALL_CHECKS
