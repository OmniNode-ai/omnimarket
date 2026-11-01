# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Eleven modules take their URLs and URI prefixes from the endpoint authority (OMN-21793)."""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_core.models.validation.model_contracts_and_di_facts import (
    ModelContractsAndDiFacts,
)
from omnibase_core.validation.checker_contracts_and_di import check_contracts_and_di
from omnibase_core.validation.validator_contracts_and_di import (
    ValidatorContractsAndDi,
)

from omnimarket.config.service_endpoints import service_endpoint_url

_ROOT = Path(__file__).resolve().parents[1]
_SRC = Path("src") / "omnimarket"
_NODES = _SRC / "nodes"

SLICE_FILES = (
    _NODES / "node_build_loop_orchestrator/handlers/adapter_delegation_router.py",
    _NODES / "node_cloud_session_placement_effect/handlers/cloud_accounts.py",
    _NODES / "node_cloud_session_placement_effect/handlers/cloud_session.py",
    _NODES / "node_contract_graph_ir_compute/handlers/handler_contract_graph_ir.py",
    _NODES / "node_dod_verify/handlers/handler_dod_evidence_github_effect.py",
    _NODES / "node_dod_verify/services/durable_evidence_gate.py",
    _NODES / "node_dod_verify/services/local_gh_reads.py",
    _NODES / "node_morning_ground_state_orchestrator/tools/effects.py",
    _NODES / "node_plans_today_effect/models/model_plans_today_schema.py",
    _NODES
    / "node_prod_promotion_grant_resolver_effect/handlers/handler_prod_promotion_grant_resolver.py",
    _NODES / "node_secret_store_effect/handlers/handler_secret_store.py",
)

_REFERENCES = {
    "claude.cloud_session_url": "claude.ai/code/",
    "pypi.release_json_template": "{distribution}/{version}/json",
    "schema_ids.json_schema_draft_2020_12": "draft/2020-12/schema",
    "internal_targets.gemini_cli": "gemini",
    "internal_targets.ui_component_prefix": "ui_component/",
    "internal_targets.secret_reference_prefix": "onex/",
}


@pytest.mark.unit
@pytest.mark.parametrize(("reference", "tail"), sorted(_REFERENCES.items()))
def test_authority_serves_each_reference(reference: str, tail: str) -> None:
    value = service_endpoint_url(reference)
    assert "://" in value
    assert tail in value


@pytest.mark.unit
def test_unknown_reference_fails_closed() -> None:
    with pytest.raises(KeyError):
        service_endpoint_url("internal_targets.no_such_target")


@pytest.mark.unit
def test_rule_fires_on_a_planted_url() -> None:
    rules = ValidatorContractsAndDi().contract.rules
    facts = ModelContractsAndDiFacts(
        repository="omnimarket",
        tag_source_repositories=frozenset(),
        advanced_sites=frozenset(),
    )
    source = 'ENDPOINT = "http://planted.invalid/v1"\n'
    issues = check_contracts_and_di(Path("src/planted.py"), source, rules, facts)
    assert [issue.code for issue in issues] == ["literal_url"]


@pytest.mark.unit
def test_slice_has_no_literal_url_findings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(_ROOT)
    validator = ValidatorContractsAndDi()
    assert any(
        rule.rule_id == "literal_url" and rule.enabled
        for rule in validator.contract.rules
    )
    assert all(path.is_file() for path in SLICE_FILES)
    result = validator.validate(list(SLICE_FILES))
    assert result.metadata is not None
    assert result.metadata.files_processed == len(SLICE_FILES)
    offenders = [
        str(issue)
        for issue in result.issues
        if issue.rule_name in {"literal_url", "unparseable_file"}
    ]
    assert offenders == []
