# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# onex-allow-file OMN-16156 reason="test fixture for the gate's own extended pattern catalog; deliberately contains every new topology-class literal as test data"
# onex-allow-internal-ip OMN-16156 reason="same as above -- whole-file exemption for tests/unit/structure/test_no_hardcoded_literals.py"
"""Tests for the generalized topology/PII pattern catalog in
``scripts/validation/check_leaked_literals.sh`` (OMN-16156, W0-GATE / G1).

G1 extends the omnimarket-only leaked-literals gate's ``LEAK_REGEX`` catalog
to the full Tier-1 topology class enumerated in
``docs/plans/2026-08-17-public-docs-kb-consolidation-plan.md`` §3: LAN
``192.168.x`` (beyond the original ``.86`` subnet), Tailscale CGNAT
(``100.64.0.0/10``), MagicDNS/tailnet hostnames (``*.tail*.ts.net``), generic
``/home/<user>/...`` paths, generic ``/Volumes/<mount>/...`` paths, the real
external cluster IP class, k8s service FQDNs (``*.svc.cluster.local``), and
``installed_by:<user>`` operator attribution.

These tests run the real gate script (unmodified subprocess invocation, no
mocking) against a throwaway git repo fixture so the assertions exercise the
actual regex, not a reimplementation of it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
GATE_SCRIPT = REPO_ROOT / "scripts" / "validation" / "check_leaked_literals.sh"

# (fixture-file content line, human label) — each must be flagged as a leak
# by the extended catalog. One literal per line so failures are unambiguous.
NEW_TOPOLOGY_CLASSES: list[tuple[str, str]] = [
    ("LAN IP outside the original .86 subnet: 192.168.1.50", "lan-ip-broadened"),
    ("Tailscale CGNAT bootstrap: 100.109.203.94:19092", "tailscale-cgnat"),
    ("MagicDNS host: omninode-pc.tail75df5e.ts.net", "magicdns-tailnet"),
    ("runner home: /home/jonah/.omnibase/runners/pool-0", "generic-home-path"),
    ("mount: /Volumes/SomeOtherDrive/Code/omni_home", "generic-volumes-mount"),
    ("external LB: 18.209.126.195", "external-cluster-ip"),
    (
        "k8s svc: omninode-valkey.data-plane.svc.cluster.local:6379",
        "k8s-service-fqdn",
    ),
    ("handshake: installed_by: jonah", "installed-by-operator"),
]


def _run_gate(cwd: Path, mode: str = "blocking") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(GATE_SCRIPT), mode, "all"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def _make_fixture_repo(tmp_path: Path, lines: list[str]) -> Path:
    repo = tmp_path / "fixture-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    doc = repo / "runbook.md"
    doc.write_text("\n".join(lines) + "\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=repo, check=True)
    return repo


@pytest.mark.unit
class TestLeakedLiteralsGateTopologyPatterns:
    """Each Tier-1 topology class must be caught by the extended catalog."""

    @pytest.mark.parametrize(
        ("line", "label"),
        NEW_TOPOLOGY_CLASSES,
        ids=[label for _, label in NEW_TOPOLOGY_CLASSES],
    )
    def test_topology_class_is_flagged(
        self, tmp_path: Path, line: str, label: str
    ) -> None:
        repo = _make_fixture_repo(tmp_path, [line])
        result = _run_gate(repo)
        assert result.returncode == 1, (
            f"expected blocking-mode gate to flag {label!r} ({line!r}) but it "
            f"exited 0.\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "findings=1" in result.stdout, result.stdout

    def test_annotated_topology_literal_is_exempt(self, tmp_path: Path) -> None:
        """A same-line annotation still exempts a topology-class literal."""
        line = (
            "Tailscale CGNAT bootstrap: 100.109.203.94:19092  "
            '# onex-allow-internal-ip OMN-16156 reason="test fixture"'
        )
        repo = _make_fixture_repo(tmp_path, [line])
        result = _run_gate(repo)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "findings=0" in result.stdout, result.stdout

    def test_advisory_mode_never_fails_on_new_classes(self, tmp_path: Path) -> None:
        """Advisory mode (G1 beta-now rollout mode) always exits 0."""
        lines = [line for line, _ in NEW_TOPOLOGY_CLASSES]
        repo = _make_fixture_repo(tmp_path, lines)
        result = _run_gate(repo, mode="advisory")
        assert result.returncode == 0, result.stdout + result.stderr
        assert f"findings={len(lines)}" in result.stdout, result.stdout
