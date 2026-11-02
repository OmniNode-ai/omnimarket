# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The CSV import schema lives in the contract, and the saving is a range (OMN-22126).

AC1: the parser reads every column name from ``config.csv_import_schema``; no
name is a literal in handler code, and a name added to a contract copy is read
with no code change. AC2: rows name their token method, totals count estimated
tokens, and the saving is a range widened by the contract's spreads, with a
basis entry per manifest entry that priced a row. AC3: the cofounder's CSV (no
model column) and the cloud owner's CSV (model plus either token spelling)
import to the same rows and totals.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from typing import Any, get_args

import pytest
import yaml
from omnibase_infra.models.pricing.model_pricing_entry import PricingConfidence
from omnibase_infra.models.pricing.model_pricing_table import ModelPricingTable

from omnimarket.nodes.node_prompt_cost_estimate_compute import (
    NOT_DELEGATED,
    EnumExportFormat,
    EnumModelSource,
    EnumTokenMethod,
    EnumUnpricedReason,
    ExportParseError,
    HandlerPromptCostEstimate,
    ModelPromptCostEstimateRequest,
    ModelPromptCostEstimateResult,
)
from omnimarket.nodes.node_prompt_cost_estimate_compute.handlers.contract_config import (
    OVERLAY_ID,
    config_from_contract,
    load_config,
    load_contract,
)
from omnimarket.nodes.node_prompt_cost_estimate_compute.models import (
    ModelPromptCostEstimateConfig,
)
from tests.node_overlay_support import install_node_overlay

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_prompt_cost_estimate_compute"
)
FIXTURES = Path(__file__).parent / "fixtures"
COFOUNDER_CSV = FIXTURES / "cofounder_no_model.csv"
OWNER_CSV = FIXTURES / "cloud_owner_tokens_in_out.csv"
OWNER_ALT_CSV = FIXTURES / "cloud_owner_prompt_completion.csv"
# The model every row of the cloud owner's fixtures names.
FIXTURE_MODEL = "gpt-4o"


def estimate(
    content: str,
    declared_model: str | None = None,
    handler: HandlerPromptCostEstimate | None = None,
) -> ModelPromptCostEstimateResult:
    return (handler or HandlerPromptCostEstimate()).handle(
        ModelPromptCostEstimateRequest(
            consent_given=True,
            export_format=EnumExportFormat.CSV,
            content=content,
            declared_model=declared_model,
        )
    )


def contract_copy() -> dict[str, Any]:
    return load_contract()


# AC1 -------------------------------------------------------------------------


def test_the_contract_declares_the_shipped_csv_schema() -> None:
    schema = load_config().csv_import_schema
    assert schema.required_columns.model_dump() == {"prompt": ("prompt",)}
    assert schema.optional_columns.model_dump() == {
        "model": ("model",),
        "conversation_id": ("conversation_id",),
        "timestamp": ("timestamp",),
        "response": ("response",),
        "tokens_in": ("tokens_in", "input_tokens", "prompt_tokens"),
        "tokens_out": ("tokens_out", "output_tokens", "completion_tokens"),
    }


def test_the_schema_block_is_plain_machine_readable_yaml() -> None:
    raw = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    block = raw["config"]["csv_import_schema"]
    assert set(block) == {"schema_version", "required_columns", "optional_columns"}
    for columns in (block["required_columns"], block["optional_columns"]):
        assert all(
            isinstance(names, list) and all(isinstance(n, str) for n in names)
            for names in columns.values()
        )


def _string_constants(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def test_no_declared_column_name_is_a_literal_in_handler_code() -> None:
    """A column or alias added to handler code instead of the contract fails here.

    The CSV reader and the handler are scanned for every declared name. The JSON
    export readers in export_parsers.py read other formats' keys (ChatGPT's own
    ``conversation_id`` key among them), so they are scanned for the required
    column and every token alias, which no JSON reader uses.
    """
    schema = load_config().csv_import_schema
    every_name = schema.accepted_names()
    token_and_required = frozenset(
        (
            *schema.required_columns.prompt,
            *schema.optional_columns.tokens_in,
            *schema.optional_columns.tokens_out,
        )
    )
    handlers = NODE_DIR / "handlers"
    offenders: list[str] = []
    for path in sorted(handlers.glob("*.py")):
        names = token_and_required if path.name == "export_parsers.py" else every_name
        offenders.extend(
            f"{path.name}:{line} {value!r}"
            for line, value in _string_constants(path)
            if value.strip().lower() in names
        )
    assert offenders == []


def test_the_literal_scan_catches_an_alias_written_into_code(tmp_path: Path) -> None:
    """Positive control: the scan sees a literal alias when one is present."""
    planted = tmp_path / "planted.py"
    planted.write_text('_CSV_TOKENS_IN = ("tokens_in", "input_tokens")\n')
    found = {value for _, value in _string_constants(planted)}
    assert found & load_config().csv_import_schema.accepted_names()


def test_an_alias_added_to_a_contract_copy_is_read_with_no_code_change() -> None:
    content = "PROMPT_TEXT,LLM,n_in,n_out\nwrite a python function,gpt-4o,1000,500\n"
    with pytest.raises(ExportParseError):
        estimate(content)

    contract = contract_copy()
    columns = contract["config"]["csv_import_schema"]
    columns["required_columns"]["prompt"].append("prompt_text")
    columns["optional_columns"]["model"].append("llm")
    columns["optional_columns"]["tokens_in"].append("n_in")
    columns["optional_columns"]["tokens_out"].append("n_out")
    config = config_from_contract(contract, environ={})
    result = estimate(content, handler=HandlerPromptCostEstimate(config_provider=lambda: config))
    (row,) = result.rows
    assert (row.source_model, row.tokens_in, row.tokens_out) == ("gpt-4o", 1000, 500)
    assert row.token_method is EnumTokenMethod.REPORTED


def test_aliases_are_read_in_declared_precedence_order() -> None:
    content = "prompt,model,prompt_tokens,tokens_in,tokens_out\nhello there,gpt-4o,7,3,2\n"
    (row,) = estimate(content).rows
    assert row.tokens_in == 3, "tokens_in is declared before prompt_tokens"

    contract = contract_copy()
    aliases = contract["config"]["csv_import_schema"]["optional_columns"]["tokens_in"]
    aliases.reverse()
    config = config_from_contract(contract, environ={})
    handler = HandlerPromptCostEstimate(config_provider=lambda: config)
    (row,) = estimate(content, handler=handler).rows
    assert row.tokens_in == 7, "the reversed contract puts prompt_tokens first"


def test_a_name_claimed_by_two_columns_is_refused() -> None:
    contract = contract_copy()
    contract["config"]["csv_import_schema"]["optional_columns"]["tokens_out"].append(
        "input_tokens"
    )
    with pytest.raises(ValueError, match="more than one column"):
        config_from_contract(contract, environ={})


# AC2 -------------------------------------------------------------------------


def test_each_row_names_how_its_tokens_were_obtained() -> None:
    content = (
        "prompt,model,tokens_in,tokens_out,response\n"
        "write a python function that parses dates,gpt-4o,100,200,\n"
        "write a python function that parses times,gpt-4o,,200,\n"
        "write a python function that parses zones,gpt-4o,100,,some answer text\n"
        "write a python function that parses years,gpt-4o,,,some answer text\n"
    )
    result = estimate(content)
    assert [row.token_method for row in result.rows] == [
        EnumTokenMethod.REPORTED,
        EnumTokenMethod.TEXT_LENGTH_INPUT,
        EnumTokenMethod.TEXT_LENGTH_OUTPUT,
        EnumTokenMethod.TEXT_LENGTH_BOTH,
    ]
    assert [row.tokens_estimated for row in result.rows] == [False, True, True, True]
    assert result.prompts_tokens_estimated == 3
    assert result.tokens_total == sum(r.tokens_in + r.tokens_out for r in result.rows)
    reported = result.rows[0]
    by_text = (
        result.rows[1].tokens_in
        + result.rows[2].tokens_out
        + result.rows[3].tokens_in
        + result.rows[3].tokens_out
    )
    assert result.tokens_from_text_length == by_text
    assert reported.tokens_in + reported.tokens_out + by_text + 200 + 100 == (
        result.tokens_total
    )
    assert result.tokens_estimated_share == Decimal(by_text) / Decimal(
        result.tokens_total
    )


def test_the_saving_is_a_range_that_holds_the_point_figure() -> None:
    result = estimate(OWNER_CSV.read_text(encoding="utf-8"))
    spread = load_config().saving_range
    assert result.saving_usd_low <= result.saving_usd <= result.saving_usd_high
    assert result.saving_usd_low < result.saving_usd_high, (
        "every manifest entry is low confidence, so the range has width"
    )
    for row in result.rows:
        assert row.saving_usd_low <= row.saving_usd <= row.saving_usd_high
        if row.recommended_tier == NOT_DELEGATED:
            assert row.saving_usd_low == row.saving_usd_high == 0
            continue
        table = ModelPricingTable.from_yaml()
        c_a = spread.confidence[table.get_entry(row.source_model).confidence]  # type: ignore[union-attr]
        c_d = spread.confidence[table.get_entry(row.recommended_model).confidence]  # type: ignore[union-attr]
        assert row.saving_usd_high == (
            row.actual_cost_usd * (1 + c_a) - row.delegated_cost_usd * (1 - c_d)
        )
    assert result.saving_usd_low == sum(row.saving_usd_low for row in result.rows)
    assert result.saving_usd_high == sum(row.saving_usd_high for row in result.rows)


def test_an_estimated_token_count_widens_the_range() -> None:
    reported = estimate(
        "prompt,model,tokens_in,tokens_out\n"
        "write a python function that parses ISO dates,gpt-4o,40,400\n"
    ).rows[0]
    reply = "x" * 1600  # 400 tokens at the text-length rate
    estimated = estimate(
        "prompt,model,tokens_in,response\n"
        f"write a python function that parses ISO dates,gpt-4o,40,{reply}\n"
    ).rows[0]
    assert (estimated.tokens_in, estimated.tokens_out) == (40, 400)
    assert estimated.saving_usd == reported.saving_usd
    assert estimated.token_method is EnumTokenMethod.TEXT_LENGTH_OUTPUT
    assert estimated.saving_usd_high > reported.saving_usd_high
    assert estimated.saving_usd_low < reported.saving_usd_low


def test_the_spread_comes_from_the_overlay_when_one_is_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    content = OWNER_CSV.read_text(encoding="utf-8")
    contract_result = estimate(content)
    zero = {level: "0" for level in get_args(PricingConfidence)}
    install_node_overlay(
        monkeypatch,
        tmp_path,
        OVERLAY_ID,
        {
            "saving_range": {
                "confidence": zero,
                "token_method": {method.value: "0" for method in EnumTokenMethod},
            }
        },
    )
    overlaid = estimate(content)
    assert overlaid.saving_usd == contract_result.saving_usd
    assert overlaid.saving_usd_low == overlaid.saving_usd == overlaid.saving_usd_high
    assert contract_result.saving_usd_low < overlaid.saving_usd_low


def test_the_spread_block_names_every_manifest_level_and_token_method() -> None:
    spread = load_config().saving_range
    assert set(spread.confidence) == set(get_args(PricingConfidence))
    assert set(spread.token_method) == set(EnumTokenMethod)
    contract = contract_copy()
    del contract["config"]["saving_range"]["confidence"]["MEASURED"]
    with pytest.raises(ValueError, match="manifest levels"):
        ModelPromptCostEstimateConfig.model_validate(contract["config"])


def test_the_basis_names_every_manifest_entry_that_priced_a_row() -> None:
    result = estimate(OWNER_CSV.read_text(encoding="utf-8"))
    table = ModelPricingTable.from_yaml()
    used = {row.source_model for row in result.rows} | {
        row.recommended_model
        for row in result.rows
        if row.recommended_tier != NOT_DELEGATED
    }
    assert {basis.model for basis in result.pricing_basis} == used
    assert len(used) > 1, "the fixture delegates at least one row"
    for basis in result.pricing_basis:
        entry = table.get_entry(basis.model)
        assert entry is not None
        assert basis.confidence == entry.confidence
        assert basis.source == entry.source
        assert basis.sample_count == entry.sample_count
        assert basis.evidence_authoritative is (
            entry.evidence.get("authoritative") is True
        )
        assert basis.checked_against_invoices is (
            basis.evidence_authoritative and entry.sample_count > 0
        )


# AC3 -------------------------------------------------------------------------


def _shape(result: ModelPromptCostEstimateResult) -> object:
    return (
        [
            row.model_dump(exclude={"model_source"})
            for row in result.rows
        ],
        result.model_dump(exclude={"rows"}),
    )


def test_the_cofounder_csv_imports_and_names_no_model() -> None:
    """Columns prompt, input_tokens, output_tokens and no model column.

    Every row is unpriced, counted under no_model_named, unless the request
    declares the model that answered them.
    """
    content = COFOUNDER_CSV.read_text(encoding="utf-8")
    undeclared = estimate(content)
    assert undeclared.rows == ()
    assert undeclared.prompts_total == 3
    assert [(g.model, g.reason, g.prompts) for g in undeclared.unpriced] == [
        ("", EnumUnpricedReason.NO_MODEL_NAMED, 3)
    ]
    declared = estimate(content, declared_model=FIXTURE_MODEL)
    assert declared.prompts_priced == 3
    assert all(row.model_source is EnumModelSource.DECLARED for row in declared.rows)


def test_both_csv_shapes_same_result() -> None:
    cofounder = estimate(
        COFOUNDER_CSV.read_text(encoding="utf-8"), declared_model=FIXTURE_MODEL
    )
    owner = estimate(OWNER_CSV.read_text(encoding="utf-8"))
    owner_alt = estimate(OWNER_ALT_CSV.read_text(encoding="utf-8"))

    assert owner.prompts_priced == 3
    assert all(row.model_source is EnumModelSource.EXPORT for row in owner.rows)
    assert all(row.token_method is EnumTokenMethod.REPORTED for row in owner.rows)
    assert [(r.tokens_in, r.tokens_out) for r in cofounder.rows] == [
        (r.tokens_in, r.tokens_out) for r in owner.rows
    ]
    assert _shape(cofounder) == _shape(owner) == _shape(owner_alt)
