# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deployed seed keeps core's error_rules document shape."""

from pathlib import Path

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_error_rule_match_compute.overlay import load_error_rule_table
from omnimarket.nodes.node_error_rule_nomatch_effect.overlay import (
    load_error_rule_nomatch_overlay,
)
from tests.unit.nodes.node_error_rule_match_compute.test_match import table


def test_core_table_document_and_explicit_version_binding_are_separate():
    snapshot = table()
    document = {"error_rules": [row.model_dump(mode="json") for row in snapshot.rows]}
    result = load_error_rule_table(
        explicit=document, key_versions=snapshot.key_versions
    )
    assert result == snapshot


def test_unconfigured_table_or_queue_refuses_before_writing():
    with pytest.raises(NodeOverlayError):
        load_error_rule_table(environ={}, key_versions=())
    with pytest.raises(NodeOverlayError):
        load_error_rule_nomatch_overlay(environ={})


def test_frozen_test_seed_goes_through_core_checker():
    import omnibase_core
    import yaml
    from omnibase_core.models.contracts.subcontracts.model_validator_subcontract import (
        ModelValidatorSubcontract,
    )
    from omnibase_core.models.error_rule_table.model_error_rule_check_input import (
        ModelErrorRuleCheckInput,
    )
    from omnibase_core.validation.checker_error_rule_table import check_error_rule_table

    path = (
        Path(omnibase_core.__file__).parent
        / "validation/contracts/error_rule_table.validation.yaml"
    )
    contract = ModelValidatorSubcontract.model_validate(
        yaml.safe_load(path.read_text())["validation"]
    )
    snapshot = table()
    document = {"error_rules": [row.model_dump(mode="json") for row in snapshot.rows]}
    request = ModelErrorRuleCheckInput(document=document, contract=contract)
    refused = check_error_rule_table(request)
    assert any("unresolved primary source" in issue.message for issue in refused)
    # This source is a synthetic fixture fact, never a deployment ruling.
    trusted_fixture = request.model_copy(
        update={"resolved_sources": frozenset({"ledger:2000-01-01T00:00:00Z"})}
    )
    assert check_error_rule_table(trusted_fixture) == ()
