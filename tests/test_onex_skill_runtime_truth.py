"""Truth-boundary checks for the source and packaged Codex skill shims."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOT = REPO_ROOT / "src" / "omnimarket" / "adapters" / "codex" / "skills"
PLUGIN_ROOT = REPO_ROOT / "plugins" / "onex" / "skills"
GENERATOR_PATH = REPO_ROOT / "scripts" / "generate_adapters.py"

EXPECTED_SKILLS = {
    "adversarial-pipeline": (
        "node_adversarial_pipeline_orchestrator",
        "adversarial_pipeline_orchestrator",
    ),
    "aislop-sweep": ("node_aislop_sweep", "aislop_sweep"),
    "bus-audit": ("node_bus_audit_compute", "bus_audit_compute"),
    "coderabbit-triage": ("node_coderabbit_triage", "coderabbit_triage"),
    "dep-cascade-dedup": (
        "node_dep_cascade_dedup_orchestrator",
        "dep_cascade_dedup_orchestrator",
    ),
    "gap": ("node_gap_compute", "gap_compute"),
    "local-review": ("node_local_review", "local_review"),
    "merge-sweep": ("node_pr_lifecycle_orchestrator", "pr_lifecycle_orchestrator"),
    "observability-sink": (
        "node_observability_sink_effect",
        "observability_sink_effect",
    ),
    "pr-polish": ("node_pr_polish", "pr_polish"),
    "recall": ("node_recall_compute", "recall_compute"),
    "session-bootstrap": ("node_session_bootstrap", "session_bootstrap"),
    "session-orchestrator": ("node_session_orchestrator", "session_orchestrator"),
    "ticket-pipeline": ("node_ticket_pipeline", "ticket_pipeline"),
}

_BEGIN = "<!-- BEGIN GENERATED CODEX RUNTIME TRUTH: do not edit -->"
_END = "<!-- END GENERATED CODEX RUNTIME TRUTH -->"
_PATTERN_B_COMMAND = "onex.cmd.omnimarket.pattern-b-dispatch.v1"
_PATTERN_B_RESPONSE = "onex.evt.omnimarket.pattern-b-dispatch-completed.v1"
SKILL_ROOTS = (PLUGIN_ROOT, SOURCE_ROOT)
LEGACY_RUNTIME_SHIM_SKILLS = {
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


def _load_generator():
    module_name = "_generate_adapters_truth_test"
    spec = importlib.util.spec_from_file_location(module_name, GENERATOR_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
    return module


def _truth_block(path: Path) -> str:
    text = path.read_text()
    assert text.count(_BEGIN) == 1, path
    assert text.count(_END) == 1, path
    return text.split(_BEGIN, 1)[1].split(_END, 1)[0]


def _contract_topics(node_name: str) -> tuple[str, str, str]:
    contract_path = (
        REPO_ROOT / "src" / "omnimarket" / "nodes" / node_name / "contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text())
    return (
        contract["name"],
        contract["event_bus"]["subscribe_topics"][0],
        contract["terminal_event"],
    )


def test_omn_12325_skills_are_runtime_adapter_shims() -> None:
    for root in SKILL_ROOTS:
        for skill_name, command_name in LEGACY_RUNTIME_SHIM_SKILLS.items():
            skill_path = root / skill_name / "SKILL.md"
            assert skill_path.exists(), f"missing skill shim: {skill_path}"
            text = skill_path.read_text(encoding="utf-8")
            assert "scripts/run_codex_runtime_request.py" in text
            assert f'--command-name "{command_name}"' in text
            assert "--compile-only" in text
            assert re.search(r"handler\s+imports", text)


def test_omn_12325_skills_do_not_contain_direct_bypass_commands() -> None:
    for root in SKILL_ROOTS:
        for skill_name in LEGACY_RUNTIME_SHIM_SKILLS:
            text = (root / skill_name / "SKILL.md").read_text(encoding="utf-8")
            for pattern in FORBIDDEN_DIRECT_BYPASS_PATTERNS:
                assert pattern.search(text) is None, (
                    f"{root / skill_name / 'SKILL.md'} contains direct bypass "
                    f"pattern {pattern.pattern!r}"
                )


@pytest.mark.unit
def test_all_fourteen_skill_pairs_share_generator_owned_truth() -> None:
    assert {path.parent.name for path in SOURCE_ROOT.glob("*/SKILL.md")} == set(
        EXPECTED_SKILLS
    )
    assert {path.parent.name for path in PLUGIN_ROOT.glob("*/SKILL.md")} == set(
        EXPECTED_SKILLS
    )

    for slug, (node_name, command_name) in EXPECTED_SKILLS.items():
        source = SOURCE_ROOT / slug / "SKILL.md"
        plugin = PLUGIN_ROOT / slug / "SKILL.md"
        source_block = _truth_block(source)
        assert source_block == _truth_block(plugin), slug

        contract_name, command_topic, terminal_topic = _contract_topics(node_name)
        assert f"**Command name:** `{command_name}`" in source_block
        assert (
            "**Request wrapper:** `scripts/run_codex_runtime_request.py`"
            in source_block
        )
        assert "**Compile-only:** pass `--compile-only`" in source_block
        assert "runtime_evidence.runtime_observation" in source_block
        assert "runtime_evidence.adapter_dispatch_binding" in source_block
        assert "runtime_evidence.schema_version" in source_block
        assert "runtime-evidence/v2" in source_block
        assert "adapter_dispatch_binding.node_contract" in source_block
        for binding_field in (
            "adapter_command_topic",
            "requested_response_topic",
            "selected_terminal_topic",
            "terminal_selection",
            "NODE_CONTRACT",
            "DIRECT_DELEGATE_SKILL_CONTRACT",
            "EXPLICIT_RESPONSE_OVERRIDE",
            "resolved, typed contract binding",
        ):
            assert binding_field in source_block
        assert "`UNOBSERVED`" in source_block
        assert "`compile_only`" in source_block
        assert f"**Backing node:** `{node_name}`" in source_block
        assert f"**Contract command name:** `{contract_name}`" in source_block
        assert f"**Contract command topic:** `{command_topic}`" in source_block
        assert f"**Contract terminal topic:** `{terminal_topic}`" in source_block

        transport = source_block.split("### Codex adapter transport", 1)[1].split(
            "### Target node contract metadata", 1
        )[0]
        if command_name == "pr_lifecycle_orchestrator":
            assert "native target-node contract route" in transport
            assert (
                f"**Adapter transport command topic:** `{command_topic}`" in transport
            )
            assert (
                f"**Adapter transport response topic:** `{terminal_topic}`" in transport
            )
        else:
            assert "generic Pattern-B adapter transport" in transport
            assert (
                f"**Adapter transport command topic:** `{_PATTERN_B_COMMAND}`"
                in transport
            )
            assert (
                f"**Adapter transport response topic:** `{_PATTERN_B_RESPONSE}`"
                in transport
            )
            assert (
                f"**Adapter transport command topic:** `{command_topic}`"
                not in transport
            )
            assert (
                f"**Adapter transport response topic:** `{terminal_topic}`"
                not in transport
            )


@pytest.mark.unit
def test_runtime_truth_blocks_are_generator_output() -> None:
    generator = _load_generator()
    specs_by_slug = {spec.slug: spec for spec in generator.CODEX_SKILL_SPECS}
    for slug, (node_name, command_name) in EXPECTED_SKILLS.items():
        contract_name, command_topic, terminal_topic = _contract_topics(node_name)
        expected = generator._render_runtime_truth_block(
            node_name=node_name,
            node_alias=command_name,
            contract_command_name=contract_name,
            contract_command_topic=command_topic,
            contract_terminal_topic=terminal_topic,
            transport_route=specs_by_slug[slug].transport_route,
        )
        assert (
            _truth_block(SOURCE_ROOT / slug / "SKILL.md")
            == expected.split(_BEGIN, 1)[1].split(_END, 1)[0]
        ), slug


@pytest.mark.unit
def test_generic_skills_never_use_target_command_as_adapter_invocation() -> None:
    for slug, (_node_name, command_name) in EXPECTED_SKILLS.items():
        if command_name == "pr_lifecycle_orchestrator":
            continue
        for root in (SOURCE_ROOT, PLUGIN_ROOT):
            text = (root / slug / "SKILL.md").read_text()
            adapter_commands = re.findall(r'--command-name "([^"]+)"', text)
            assert adapter_commands
            assert set(adapter_commands) == {command_name}
            transport = (
                _truth_block(root / slug / "SKILL.md")
                .split("### Codex adapter transport", 1)[1]
                .split("### Target node contract metadata", 1)[0]
            )
            assert "target node command topic directly" not in transport
            assert _PATTERN_B_COMMAND in transport


@pytest.mark.unit
def test_public_canonical_generator_is_exact_and_idempotent(tmp_path: Path) -> None:
    generator = _load_generator()
    assert {
        spec.slug: (spec.node_name, spec.command_name)
        for spec in generator.CODEX_SKILL_SPECS
    } == EXPECTED_SKILLS
    assert (
        sum(
            spec.transport_route == "native_contract"
            for spec in generator.CODEX_SKILL_SPECS
        )
        == 1
    )
    contracts_root = tmp_path / "nodes"
    output_root = tmp_path / "skills"
    for spec in generator.CODEX_SKILL_SPECS:
        node_dir = contracts_root / spec.node_name
        node_dir.mkdir(parents=True)
        command_topic = f"onex.cmd.omnimarket.{spec.slug}-start.v9"
        terminal_topic = f"onex.evt.omnimarket.{spec.slug}-completed.v9"
        (node_dir / "contract.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": spec.command_name,
                    "description": f"{spec.command_name} test node",
                    "descriptor": {"timeout_ms": 30000},
                    "inputs": {},
                    "event_bus": {"subscribe_topics": [command_topic]},
                    "terminal_event": terminal_topic,
                }
            )
        )

    generated = generator.generate_canonical_codex_skills(
        output_root, contracts_root=contracts_root
    )
    assert generated == tuple(
        sorted(
            output_root / spec.slug / "SKILL.md" for spec in generator.CODEX_SKILL_SPECS
        )
    )
    assert {path.parent.name for path in output_root.glob("*/SKILL.md")} == set(
        EXPECTED_SKILLS
    )
    first_run = {path: path.read_bytes() for path in generated}

    for spec in generator.CODEX_SKILL_SPECS:
        text = (output_root / spec.slug / "SKILL.md").read_text()
        assert f"**Command name:** `{spec.command_name}`" in text
        assert "scripts/run_codex_runtime_request.py" in text
        assert "--compile-only" in text
        command_topic = f"onex.cmd.omnimarket.{spec.slug}-start.v9"
        terminal_topic = f"onex.evt.omnimarket.{spec.slug}-completed.v9"
        assert f"**Contract command topic:** `{command_topic}`" in text
        assert f"**Contract terminal topic:** `{terminal_topic}`" in text
        if spec.transport_route == "native_contract":
            assert "native target-node contract route" in text
            transport = text.split("### Codex adapter transport", 1)[1].split(
                "### Target node contract metadata", 1
            )[0]
            assert (
                f"**Adapter transport command topic:** `{command_topic}`" in transport
            )
            assert (
                f"**Adapter transport response topic:** `{terminal_topic}`" in transport
            )
            assert _PATTERN_B_COMMAND not in transport
        else:
            assert "generic Pattern-B adapter transport" in text
            transport = text.split("### Codex adapter transport", 1)[1].split(
                "### Target node contract metadata", 1
            )[0]
            assert _PATTERN_B_COMMAND in transport
            assert _PATTERN_B_RESPONSE in transport

    generator.generate_canonical_codex_skills(
        output_root, contracts_root=contracts_root
    )
    assert first_run == {path: path.read_bytes() for path in generated}
    generator.generate_canonical_codex_skills(
        output_root, contracts_root=contracts_root, check=True
    )
    preserved = generated[1]
    preserved.write_text(preserved.read_text() + "\nhand-authored detail\n")
    generator.generate_canonical_codex_skills(
        output_root, contracts_root=contracts_root, check=True
    )
    assert "hand-authored detail" in preserved.read_text()

    drifted = generated[0]
    drifted.write_text(
        drifted.read_text().replace(
            "**Command name:** `adversarial_pipeline_orchestrator`",
            "**Command name:** `wrong_command`",
        )
    )
    with pytest.raises(RuntimeError, match="runtime-truth drift"):
        generator.generate_canonical_codex_skills(
            output_root, contracts_root=contracts_root, check=True
        )


@pytest.mark.unit
def test_public_canonical_generator_rejects_unlisted_skill_paths(
    tmp_path: Path,
) -> None:
    generator = _load_generator()
    contracts_root = tmp_path / "nodes"
    for spec in generator.CODEX_SKILL_SPECS:
        node_dir = contracts_root / spec.node_name
        node_dir.mkdir(parents=True)
        (node_dir / "contract.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": spec.command_name,
                    "event_bus": {
                        "subscribe_topics": [
                            f"onex.cmd.omnimarket.{spec.slug}-start.v1"
                        ]
                    },
                    "terminal_event": f"onex.evt.omnimarket.{spec.slug}-completed.v1",
                }
            )
        )
    output_root = tmp_path / "skills"
    extra = output_root / "not-in-manifest" / "SKILL.md"
    extra.parent.mkdir(parents=True)
    extra.write_text("extra")
    with pytest.raises(ValueError, match="unlisted skill paths"):
        generator.generate_canonical_codex_skills(
            output_root, contracts_root=contracts_root
        )


@pytest.mark.unit
def test_manifest_contract_path_is_authoritative(tmp_path: Path) -> None:
    generator = _load_generator()
    contracts_root = tmp_path / "nodes"
    for spec in generator.CODEX_SKILL_SPECS:
        node_dir = contracts_root / spec.node_name
        node_dir.mkdir(parents=True)
        (node_dir / "contract.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": spec.command_name,
                    "event_bus": {
                        "subscribe_topics": [
                            f"onex.cmd.omnimarket.{spec.slug}-start.v1"
                        ]
                    },
                    "terminal_event": f"onex.evt.omnimarket.{spec.slug}-completed.v1",
                }
            )
        )

    first = generator.CODEX_SKILL_SPECS[0]
    mismatched = generator.CodexSkillSpec(
        first.slug,
        first.command_name,
        first.node_name,
        "src/omnimarket/nodes/node_unlisted/contract.yaml",
        first.transport_route,
    )
    bad_specs = (mismatched, *generator.CODEX_SKILL_SPECS[1:])
    with pytest.raises(ValueError, match="invalid repo-relative contract path"):
        generator.generate_canonical_codex_skills(
            tmp_path / "skills", contracts_root=contracts_root, specs=bad_specs
        )
