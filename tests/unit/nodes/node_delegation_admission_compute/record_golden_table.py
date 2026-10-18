# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Record the admission golden table from omniclaude's own code (OMN-21097).

Not a test. Run by hand against an omniclaude checkout to re-record
``fixtures/golden_admission_table.json``:

    uv run python tests/unit/nodes/node_delegation_admission_compute/record_golden_table.py \
        --omniclaude <path to an omniclaude checkout> --sha <its commit>

For each recorded Agent input it runs omniclaude's ``SensitivityGate`` and
``TaskClassifier.is_delegatable`` over ``json.dumps(tool_input)`` (the
delegation hook runner's flattening) and the agent-model guard's
``check_agent_input`` with the shipped allowlist and routing contract, and
stores their verdicts. The admission compute must reproduce them.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
import types
from pathlib import Path
from typing import Any

_ROUTE_SONNET = (
    "ROUTE: band=B3 score=9 dims=S2A1R1V2N1C1J1 dest=lane model=sonnet effort=high"
)
_ROUTE_OPUS = (
    "ROUTE: band=B4 score=14 dims=S2A2R2V2N2C2J2 dest=lane model=opus effort=high"
)

# (id, description, task text) for the classifier rows. Each becomes a
# foreground, model-raised Agent call that the guard allows.
_CLASSIFIER_ROWS: tuple[tuple[str, str, str], ...] = (
    (
        "research-summarize",
        "summarize module",
        "Research and summarize how the routing reducer folds events.",
    ),
    (
        "research-explain",
        "explain design",
        "Explain the design of the event bus wiring and compare the two options.",
    ),
    (
        "research-investigate",
        "investigate behaviour",
        "Investigate why the projection lags and analyze the likely causes.",
    ),
    (
        "research-review",
        "review a plan",
        "Review this plan and summarize the open questions.",
    ),
    (
        "research-what-is",
        "what is the contract",
        "What is the purpose of the contract handler_routing section? Explain it.",
    ),
    (
        "research-find",
        "find usages",
        "Find where the size band thresholds are defined and explain them.",
    ),
    (
        "research-compare",
        "compare approaches",
        "Compare a reducer and an orchestrator for this workflow and analyze tradeoffs.",
    ),
    (
        "research-understand",
        "understand flow",
        "Help me understand the delegation flow end to end.",
    ),
    (
        "document-write-docs",
        "write docs",
        "Write documentation for the admission policy fields.",
    ),
    (
        "document-docstring",
        "add docstrings",
        "Add docstrings to every public function and document the parameters.",
    ),
    (
        "document-readme",
        "draft readme",
        "Generate docs for the README describing setup and usage.",
    ),
    (
        "document-changelog",
        "changelog entry",
        "Document the change in a changelog entry with a short summary.",
    ),
    (
        "document-create-doc",
        "create doc",
        "Create doc pages that explain each reason code.",
    ),
    (
        "test-write-tests",
        "write tests",
        "Write tests for the sensitivity gate covering every pattern.",
    ),
    (
        "test-unit",
        "write unit test",
        "Write unit test cases for the classifier confidence arithmetic.",
    ),
    ("test-add", "add tests", "Add test coverage for the empty input case."),
    ("test-generate", "generate tests", "Generate test fixtures for the golden table."),
    (
        "test-pytest",
        "pytest cases",
        "Create tests with pytest parametrize for each band.",
    ),
    (
        "implement-create",
        "create handler",
        "Create a new handler that reads the policy and returns a verdict.",
    ),
    (
        "implement-build",
        "build node",
        "Build the ONEX compute node with its contract and models.",
    ),
    (
        "implement-add-feature",
        "add feature",
        "Implement a feature flag for the cohort lookup.",
    ),
    (
        "implement-domain",
        "domain only",
        "ONEX authentication system with an effect node and a reducer.",
    ),
    (
        "debug-error",
        "fix error",
        "Debug the error in the consumer and fix the failing import.",
    ),
    (
        "debug-traceback",
        "traceback",
        "Why does this traceback happen? The test fails with a KeyError bug.",
    ),
    (
        "debug-broken",
        "broken build",
        "The build is broken; troubleshoot the issue and fix it.",
    ),
    (
        "refactor-cleanup",
        "refactor",
        "Refactor the handler to remove duplication and clean up naming.",
    ),
    ("refactor-rename", "rename", "Restructure the module and simplify the helpers."),
    (
        "database-migration",
        "migration",
        "Write a SQL migration that adds an index to the claims table.",
    ),
    (
        "database-query",
        "query",
        "Optimize the postgres query and the schema for the projection table.",
    ),
    (
        "vision-screenshot",
        "screenshot",
        "Look at this screenshot and describe the image.",
    ),
    (
        "vision-diagram",
        "diagram",
        "Summarize the architecture diagram in the attached figure.",
    ),
    ("tool-run-tests", "run tests", "Run the tests and report which fail."),
    ("tool-git-commit", "commit", "Commit the change with git and push the branch."),
    ("tool-deploy", "deploy", "Deploy the service to the lab and check the logs."),
    ("tool-execute", "execute", "Execute the migration script against the database."),
    ("tool-shell", "shell", "Open a shell and list the files in the directory."),
    ("unknown-greeting", "hello", "Hello there."),
    ("unknown-vague", "thoughts", "Thoughts?"),
    (
        "mixed-research-implement",
        "research then build",
        "Research the options, then implement the chosen one.",
    ),
    (
        "mixed-test-debug",
        "tests and debug",
        "Write tests that reproduce the bug and debug the failure.",
    ),
    (
        "long-research",
        "long research",
        "Research, analyze, summarize and explain the tradeoffs between the three caching strategies, and review the evidence for each one.",
    ),
    (
        "long-document",
        "long document",
        "Write documentation and document every endpoint, with docstrings and a readme section for each one.",
    ),
    ("opus-research", "opus research", "Research and summarize the routing tiers."),
    ("opus-document", "opus document", "Write documentation for the routing tiers."),
)

# (id, tool_input) for the guard rows: the guard's own Agent fixtures
# (tests/hooks/test_workflow_model_guard.py), as foreground Agent calls.
_GUARD_ROWS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("guard-fork", {"subagent_type": "fork", "description": "continue the lane"}),
    ("guard-fork-opus", {"subagent_type": "fork", "model": "opus", "description": "x"}),
    (
        "guard-sonnet-no-route",
        {
            "subagent_type": "general-purpose",
            "model": "sonnet",
            "description": "do a thing",
        },
    ),
    ("guard-opus-no-route", {"subagent_type": "general-purpose", "model": "opus"}),
    ("guard-haiku", {"subagent_type": "general-purpose", "model": "haiku"}),
    (
        "guard-haiku-full",
        {"subagent_type": "general-purpose", "model": "claude-haiku-4-5-20251001"},
    ),
    (
        "guard-no-model",
        {"subagent_type": "general-purpose", "description": "do a thing"},
    ),
    ("guard-fable", {"subagent_type": "general-purpose", "model": "fable"}),
    (
        "guard-route-b3-ok",
        {
            "subagent_type": "general-purpose",
            "model": "sonnet",
            "description": "d",
            "prompt": _ROUTE_SONNET + "\nwork",
        },
    ),
    (
        "guard-route-b4-sonnet",
        {
            "subagent_type": "general-purpose",
            "model": "sonnet",
            "description": "d",
            "prompt": _ROUTE_OPUS,
        },
    ),
    (
        "guard-route-unparsable",
        {
            "subagent_type": "general-purpose",
            "model": "sonnet",
            "prompt": "ROUTE: band=B3 model=sonnet\ndo a thing",
        },
    ),
    (
        "guard-route-in-description",
        {
            "subagent_type": "general-purpose",
            "model": "sonnet",
            "description": _ROUTE_SONNET,
            "prompt": "do a thing",
        },
    ),
    (
        "guard-floor-r3",
        {
            "subagent_type": "general-purpose",
            "model": "sonnet",
            "prompt": "ROUTE: band=B3 score=9 dims=S1A1R3V1N1C1J1 dest=lane model=sonnet effort=high\ndo a thing",
        },
    ),
    (
        "guard-floor-j3",
        {
            "subagent_type": "general-purpose",
            "model": "sonnet",
            "prompt": "ROUTE: band=B3 score=9 dims=S1A1R1V1N1C1J3 dest=lane model=sonnet effort=high\ndo a thing",
        },
    ),
    (
        "guard-floor-b4-low-score",
        {
            "subagent_type": "general-purpose",
            "model": "opus",
            "prompt": "ROUTE: band=B4 score=3 dims=S0A0R3V0N0C0J0 dest=lane model=opus effort=high\ndo a thing",
        },
    ),
    (
        "guard-score-sum",
        {
            "subagent_type": "general-purpose",
            "model": "sonnet",
            "prompt": _ROUTE_SONNET.replace("score=9", "score=10") + "\ndo a thing",
        },
    ),
    (
        "guard-below-score-band",
        {
            "subagent_type": "general-purpose",
            "model": "sonnet",
            "prompt": _ROUTE_OPUS.replace("band=B4", "band=B3").replace(
                "model=opus", "model=sonnet"
            )
            + "\ndo a thing",
        },
    ),
    (
        "guard-effort",
        {
            "subagent_type": "general-purpose",
            "model": "opus",
            "prompt": "ROUTE: band=B5 score=18 dims=S3A3R2V2N3C2J3 dest=lane model=opus effort=low\ndo a thing",
        },
    ),
    (
        "guard-band-b0",
        {
            "subagent_type": "general-purpose",
            "model": "sonnet",
            "prompt": "ROUTE: band=B0 score=2 dims=S1A1R0V0N0C0J0 dest=x model=sonnet effort=high\ndo a thing",
        },
    ),
    (
        "guard-band-b1",
        {
            "subagent_type": "general-purpose",
            "model": "sonnet",
            "prompt": "ROUTE: band=B1 score=2 dims=S1A1R0V0N0C0J0 dest=x model=sonnet effort=high\ndo a thing",
        },
    ),
    (
        "guard-band-b1c",
        {
            "subagent_type": "general-purpose",
            "model": "sonnet",
            "prompt": "ROUTE: band=B1c score=2 dims=S1A1R0V0N0C0J0 dest=x model=sonnet effort=high\ndo a thing",
        },
    ),
    (
        "guard-band-model",
        {
            "subagent_type": "general-purpose",
            "model": "opus",
            "prompt": "ROUTE: band=B3 score=9 dims=S2A1R1V2N1C1J1 dest=lane model=opus effort=high\ndo a thing",
        },
    ),
    (
        "guard-floor-b3-r3",
        {
            "subagent_type": "general-purpose",
            "model": "sonnet",
            "prompt": "ROUTE: band=B3 score=11 dims=S2A1R3V2N1C1J1 dest=lane model=sonnet effort=high\ndo a thing",
        },
    ),
)


def _load(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _classifier_input(description: str, text: str, model: str) -> dict[str, Any]:
    route = _ROUTE_OPUS if model == "opus" else _ROUTE_SONNET
    return {
        "description": description,
        "prompt": f"{route}\n{text}",
        "subagent_type": "general-purpose",
        "model": model,
        "run_in_background": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--omniclaude", type=Path, required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).parent / "fixtures" / "golden_admission_table.json",
    )
    args = parser.parse_args()
    root: Path = args.omniclaude

    # The classifier resolves a delegate model from a bifrost contract at import;
    # admission never uses it, so a one-backend stand-in satisfies the import.
    with tempfile.TemporaryDirectory() as tmp:
        bifrost = Path(tmp) / "bifrost_delegation.yaml"
        bifrost.write_text(
            "backends:\n  - {backend_id: stand-in, tier: local, capabilities: [code_generation]}\n",
            encoding="utf-8",
        )
        runner = types.ModuleType("omniclaude.delegation.runner")
        runner.__dict__["canonical_bifrost_contract_path"] = lambda: bifrost
        for name in ("omniclaude", "omniclaude.delegation", "omniclaude.lib"):
            sys.modules.setdefault(name, types.ModuleType(name))
        sys.modules["omniclaude.delegation.runner"] = runner
        classifier_mod = _load(
            "omniclaude.lib.task_classifier",
            root / "src/omniclaude/lib/task_classifier.py",
        )
        gate_mod = _load(
            "omniclaude.delegation.sensitivity_gate",
            root / "src/omniclaude/delegation/sensitivity_gate.py",
        )
    guard = _load(
        "workflow_model_guard", root / "plugins/onex/hooks/lib/workflow_model_guard.py"
    )
    allowlist = guard.load_allowlist()
    routing = guard.load_routing()
    classifier = classifier_mod.TaskClassifier()
    gate = gate_mod.SensitivityGate()

    inputs: list[tuple[str, str, dict[str, Any]]] = []
    for row_id, description, text in _CLASSIFIER_ROWS:
        model = "opus" if row_id.startswith("opus-") else "sonnet"
        inputs.append(
            ("classifier", row_id, _classifier_input(description, text, model))
        )
    for row_id, tool_input in _GUARD_ROWS:
        inputs.append(("guard", row_id, {**tool_input, "run_in_background": False}))

    rows = []
    for group, row_id, tool_input in inputs:
        text = json.dumps(tool_input)
        score = classifier.is_delegatable(text)
        findings = guard.check_agent_input(tool_input, allowlist, routing=routing)
        rows.append(
            {
                "id": row_id,
                "group": group,
                "tool_input": tool_input,
                "omniclaude": {
                    "guard_reason": findings[0].reason if findings else None,
                    "sensitive": gate.check(text).is_sensitive,
                    "delegatable": score.delegatable,
                    "classified_intent": score.classified_intent,
                },
            }
        )
    table = {
        "recorded_from": f"omniclaude@{args.sha}",
        "flattening": "json.dumps(tool_input)",
        "rows": rows,
    }
    args.out.write_text(json.dumps(table, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(rows)} rows to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
