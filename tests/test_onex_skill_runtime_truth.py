"""Regression tests for OMN-12325 Codex/ONEX skill runtime truth surfaces."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOTS = (
    REPO_ROOT / "plugins" / "onex" / "skills",
    REPO_ROOT / "src" / "omnimarket" / "adapters" / "codex" / "skills",
)
OMN_12325_SKILLS = {
    "recall": "recall_compute",
    "observability-sink": "observability_sink_effect",
    "dep-cascade-dedup": "dep_cascade_dedup_orchestrator",
    "adversarial-pipeline": "adversarial_pipeline_orchestrator",
}
FORBIDDEN_DIRECT_BYPASS_PATTERNS = (
    re.compile(r"\bfrom\s+omnimarket\.nodes\b"),
    re.compile(r"\bimport\s+httpx\b"),
    re.compile(r"\bimport\s+requests\b"),
    re.compile(r"\bsubprocess\."),
    re.compile(r"\b(?:gh|curl)\s+(?:api|pr|repo)\b"),
    re.compile(r"\.handle\("),
    re.compile(r"localhost:8085"),
)


def test_omn_12325_skills_are_runtime_adapter_shims() -> None:
    for root in SKILL_ROOTS:
        for skill_name, command_name in OMN_12325_SKILLS.items():
            skill_path = root / skill_name / "SKILL.md"
            assert skill_path.exists(), f"missing skill shim: {skill_path}"
            text = skill_path.read_text(encoding="utf-8")
            assert "scripts/run_codex_runtime_request.py" in text
            assert f'--command-name "{command_name}"' in text
            assert "--compile-only" in text
            assert re.search(r"handler\s+imports", text)


def test_omn_12325_skills_do_not_contain_direct_bypass_commands() -> None:
    for root in SKILL_ROOTS:
        for skill_name in OMN_12325_SKILLS:
            skill_path = root / skill_name / "SKILL.md"
            text = skill_path.read_text(encoding="utf-8")
            for pattern in FORBIDDEN_DIRECT_BYPASS_PATTERNS:
                assert pattern.search(text) is None, (
                    f"{skill_path} contains direct bypass pattern {pattern.pattern!r}"
                )
