# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20161 - the OCC gate caller pins must resolve the writer-app pin-only exemption.

omnibase_core#1820 exempts the OCC writer app from ``occ-preflight.yml`` and
``receipt-gate.yml`` only when the pin-only probe proves the producer's outcome.
Callers here pin both reusable workflows by sha, so the exemption reaches this
repository only once every pin resolves a workflow that carries it. This test
follows the chain: caller pin, then the pinned workflow text.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
REQUIRED_CHECKS_PATH = REPO_ROOT / ".github" / "required-checks.yaml"
CORE = "OmniNode-ai/omnibase_core"
EXPECTED_SHA = "52851458622f368c3b596c82bf810bc6acce1d5e"  # pragma: allowlist secret
GATE_FILES = ("occ-preflight.yml", "receipt-gate.yml")
WRITER_APP = "onexbot-occ-writer"
PIN_ONLY_PROBE = "--check-no-companion-required"

_REF_RE = re.compile(
    rf"{re.escape(CORE)}/\.github/workflows/(?P<file>occ-preflight|receipt-gate)\.yml"
    r"@(?P<ref>[^\s\"'#]+)"
)


def _sha_pins() -> list[tuple[str, str, str]]:
    """Every (source, workflow file, ref) that names an OCC gate reusable by ref."""
    sources = [*sorted(WORKFLOWS_DIR.glob("*.yml")), REQUIRED_CHECKS_PATH]
    found: list[tuple[str, str, str]] = []
    for path in sources:
        for line in path.read_text().splitlines():
            if line.lstrip().startswith("#"):
                continue
            for match in _REF_RE.finditer(line):
                found.append((path.name, f"{match['file']}.yml", match["ref"]))
    return found


def _sha_pins_only() -> list[tuple[str, str, str]]:
    return [p for p in _sha_pins() if re.fullmatch(r"[0-9a-f]{40}", p[2])]


def _fetch_core_file(ref: str, path: str) -> str:
    url = f"https://api.github.com/repos/{CORE}/contents/{path}?ref={ref}"
    try:
        done = subprocess.run(
            ["gh", "api", url, "-H", "Accept: application/vnd.github.raw"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        done = None
    if done is None or done.returncode != 0:
        pytest.skip(f"omnibase_core {path}@{ref} is not readable from this environment")
    return done.stdout


def test_both_gate_callers_are_found() -> None:
    """Positive control: the scan sees both reusables, so an empty scan cannot pass."""
    assert {file for _, file, _ in _sha_pins_only()} == set(GATE_FILES)


def test_workflow_callers_use_yaml_uses_keys() -> None:
    """Every caller workflow pins through a real ``uses`` key, not prose."""
    for name, gate in (
        ("call-occ-preflight.yml", "occ-preflight.yml"),
        ("call-receipt-gate.yml", "receipt-gate.yml"),
    ):
        data = yaml.safe_load((WORKFLOWS_DIR / name).read_text())
        uses = [
            str(job["uses"])
            for job in data["jobs"].values()
            if f"/{gate}@" in str(job.get("uses", ""))
        ]
        assert len(uses) == 1, f"{name}: expected one {gate} caller, got {uses}"


def test_every_occ_preflight_pin_is_the_expected_sha() -> None:
    stale = [
        p
        for p in _sha_pins_only()
        if p[1] == "occ-preflight.yml" and p[2] != EXPECTED_SHA
    ]
    assert not stale, (
        f"occ-preflight pins not at {EXPECTED_SHA}: {stale}. The writer-app pin-only "
        "exemption (omnibase_core#1820) reaches this repo only when they move."
    )


def test_occ_preflight_pins_move_together() -> None:
    refs = {ref for _, file, ref in _sha_pins_only() if file == "occ-preflight.yml"}
    assert len(refs) == 1, f"occ-preflight pins diverge: {sorted(refs)}"


@pytest.mark.parametrize("gate", GATE_FILES)
def test_pinned_workflow_carries_writer_app_pin_only_exemption(gate: str) -> None:
    # The receipt-gate caller advances past #1820 (OMN-20375): read each gate at its own pins.
    refs = {ref for _, file, ref in _sha_pins_only() if file == gate}
    assert refs, f"no sha pin of {gate} found"
    for ref in sorted(refs):
        text = _fetch_core_file(ref, f".github/workflows/{gate}")
        assert WRITER_APP in text, f"{gate}@{ref} lacks the writer app exemption"
        assert PIN_ONLY_PROBE in text, f"{gate}@{ref} lacks the pin-only probe"


@pytest.mark.parametrize("gate", GATE_FILES)
def test_floor_rejects_a_pin_without_the_exemption(gate: str) -> None:
    """Positive control: the parent of omnibase_core#1820's squash lacks it."""
    text = _fetch_core_file(f"{EXPECTED_SHA}~1", f".github/workflows/{gate}")
    assert WRITER_APP not in text
