# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime and canonical routing audit consume the same overlay authority."""

from pathlib import Path

import pytest
import yaml

from scripts.validation.validate_contract_overlay_boundary import (
    validate_contract_overlay_boundary,
)

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = Path("src/omnimarket/nodes/node_generation_consumer/contract.yaml")


def test_base_contract_and_resolved_routing_satisfy_both_rules() -> None:
    assert validate_contract_overlay_boundary(ROOT, [ROOT / CONTRACT]) == []
    from omnibase_core.models.validation.model_routing_contract_entry import (
        ModelRoutingContractEntry,
    )

    from omnimarket.handlers.handler_contract_routing_overlay import (
        load_routing_contract,
    )

    raw = yaml.safe_load((ROOT / CONTRACT).read_text())
    resolved = load_routing_contract(ROOT / CONTRACT)
    assert "served_model_id" not in raw.get("model_routing", {})
    for key in ModelRoutingContractEntry(
        contract_rel=str(CONTRACT)
    ).required_routing_keys:
        assert resolved["model_routing"][key]


@pytest.mark.parametrize(
    "reference", ["missing.yaml", "../outside.yaml", "/tmp/outside.yaml"]
)
def test_unreadable_or_escaping_overlay_fails_closed(
    tmp_path: Path, reference: str
) -> None:
    from omnimarket.handlers.handler_contract_routing_overlay import (
        load_routing_contract,
    )

    contract = tmp_path / "contract.yaml"
    contract.write_text(yaml.safe_dump({"model_routing_overlay": reference}))
    with pytest.raises((ValueError, FileNotFoundError)):
        load_routing_contract(contract)


def test_base_cannot_override_overlay_model(tmp_path: Path) -> None:
    from omnimarket.handlers.handler_contract_routing_overlay import (
        load_routing_contract,
    )

    contract = tmp_path / "contract.yaml"
    contract.write_text(
        "model_routing_overlay: routing.yaml\nmodel_routing: {served_model_id: shadow}\n"
    )
    (tmp_path / "routing.yaml").write_text(
        "model_routing: {served_model_id: authority}\n"
    )
    with pytest.raises(ValueError, match="both"):
        load_routing_contract(contract)


def test_runtime_uses_the_declared_overlay() -> None:
    from omnimarket.handlers.handler_contract_routing_overlay import (
        load_routing_contract,
    )
    from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
        HandlerGenerationConsumer,
    )

    route = load_routing_contract(ROOT / CONTRACT)["model_routing"]
    handler = HandlerGenerationConsumer(effect_handler=object())
    assert (
        handler._provider,
        handler._served_model_id,
        handler._endpoint_ref,
        handler._routing_source,
    ) == tuple(
        route[k]
        for k in ("provider", "served_model_id", "endpoint_ref", "routing_source")
    )


@pytest.mark.parametrize(
    "key", ["provider", "served_model_id", "endpoint_ref", "routing_source"]
)
def test_canonical_audit_still_requires_every_routing_field(
    tmp_path: Path, key: str
) -> None:
    from omnibase_core.validation.checker_routing_authority import (
        check_routing_authority_at_path,
    )

    overlay = tmp_path / "routing_overlay.yaml"
    route = {
        "provider": "local",
        "served_model_id": "runtime-owned",
        "endpoint_ref": "local-coder",
        "routing_source": "overlay",
    }
    del route[key]
    overlay.write_text(yaml.safe_dump({"model_routing": route}))
    (tmp_path / "bifrost.yaml").write_text(
        "backends:\n  - backend_id: local-coder\n    tier: local\n    endpoint_url_env: LOCAL_URL\n"
    )
    report = check_routing_authority_at_path(
        repo_root=tmp_path,
        demo_path_contracts=("routing_overlay.yaml",),
        demo_path_sources=(),
        bifrost_config_rel="bifrost.yaml",
    )
    assert not report.passed
    assert (
        f"routing_overlay.yaml: model_routing.{key} is absent/blank"
        in report.positive_proof["errors"]
    )


def test_gate_checks_base_then_exact_runtime_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.validation import validate_contract_overlay_boundary as gate

    contract = tmp_path / CONTRACT
    contract.parent.mkdir(parents=True)
    contract.write_text("model_routing_overlay: chosen.yaml\n")
    (contract.parent / "chosen.yaml").write_text(
        "model_routing: {served_model_id: runtime-owned}\n"
    )
    calls = []
    monkeypatch.setattr(gate, "core_main", lambda args: calls.append(args) or 0)
    assert gate.main(["--routing-authority", "--root", str(tmp_path)]) == 0
    assert calls == [
        [
            "--repo-root",
            str(tmp_path.resolve()),
            "--contract",
            str(CONTRACT.parent / "chosen.yaml"),
        ]
    ]
    # An inline model can never shadow the audited resource.
    contract.write_text(
        "model_routing_overlay: chosen.yaml\nmodel_routing: {served_model_id: shadow}\n"
    )
    assert gate.main(["--routing-authority", "--root", str(tmp_path)]) == 1
    assert len(calls) == 1
