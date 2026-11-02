# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# Rewrote test_invalid_claim_absent_from_buffer_all_entry_points_claim_contract as a payload check.
# Dropped its caller/write checks: require the retired append command, CLI verbs and bus publishers.
"""Opt-in CLAIM evidence contracts and behavior-proving checks."""

from __future__ import annotations

import json

import pytest

from omnimarket.handlers import ledger_claim_contract as claim_contract
from omnimarket.handlers import ledger_row_grammar as g

pytestmark = pytest.mark.unit
FLAG = claim_contract.REQUIRE_CLAIM_CONTRACT_ENV
STAMP = "2026-09-23T14:00:00Z"
PLAIN_CLAIM = (
    f"{STAMP} | CLAIM | lane=lane-a | actor=claude:opus5.5:subagent | ticket=OMN-1 | scope=x "
    "| worktree=none | est ~1 lane-hours; displaces nothing; (OMN-1)"
)


def _item(
    kind: str = "command", value: object = "pytest tests/x.py -q"
) -> dict[str, object]:
    return {
        "id": "dod-tests",
        "description": "The changed behavior passes its tests",
        "source": "manual",
        "binds_ac": ["ac-1"],
        "checks": [{"check_type": kind, "check_value": value, "cwd": None}],
        "execution_scope": "local",
        "status": "pending",
        "evidence_artifact": None,
        "linear_dod_text": None,
    }


def _contract(items: object, schema: str | None = "1.0.0") -> str:
    cells = f" | dod-evidence={json.dumps(items).replace('|', chr(166))}"
    if schema is not None:
        cells += f" | contract-schema={schema}"
    return cells


def _reason(row: str) -> str:
    reasons = g.row_refusals(row)
    assert len(reasons) == 1, reasons
    assert reasons[0].startswith("OMN-20029: ")
    return reasons[0]


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "Yes", "ON"])
def test_enabled_values_claim_contract(value: str) -> None:
    assert claim_contract.enforced({FLAG: value})


@pytest.mark.parametrize(
    "value", ["0", "false", "", "off", "no", "enabled", "2", " true "]
)
def test_disabled_values_claim_contract(value: str) -> None:
    assert not claim_contract.enforced({FLAG: value})
    assert not claim_contract.enforced({})


@pytest.mark.parametrize(
    ("cells", "reason"),
    [
        ("", "missing dod-evidence"),
        (" | dod-evidence=", "dod-evidence= is empty"),
        (" | dod-evidence=  ", "dod-evidence= is empty"),
        (" | dod-evidence={broken", "malformed JSON"),
        (_contract({}), "must be a JSON array"),
        (_contract(None), "must be a JSON array"),
        (_contract([]), "empty array"),
        (_contract([42]), "dod-evidence[0] must be an object"),
        (_contract([_item()]) + " | dod-evidence=[]", "occurs more than once"),
        (_contract([_item("unknown")]), "check_type"),
        (_contract([_item()], schema=None), "contract-schema"),
    ],
)
def test_bad_cells_refused_claim_contract(
    monkeypatch: pytest.MonkeyPatch, cells: str, reason: str
) -> None:
    monkeypatch.setenv(FLAG, "on")
    assert reason in _reason(PLAIN_CLAIM + cells)


@pytest.mark.parametrize(
    "schema", ["", "1", "1.0", "v1.0.0", "1.0.0-beta", "\u0661.0.0"]
)
def test_schema_digits_only_claim_contract(
    monkeypatch: pytest.MonkeyPatch, schema: str
) -> None:
    monkeypatch.setenv(FLAG, "on")
    assert "contract-schema" in _reason(PLAIN_CLAIM + _contract([_item()], schema))


def test_duplicate_schema_refused_claim_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(FLAG, "on")
    assert "contract-schema" in _reason(
        PLAIN_CLAIM + _contract([_item()]) + " | contract-schema=1.0.0"
    )


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("id", None, ".id"),
        ("id", " ", ".id"),
        ("id", 1, ".id"),
        ("description", None, ".description"),
        ("description", "", ".description"),
        ("description", 1, ".description"),
        ("checks", None, ".checks"),
        ("checks", {}, ".checks"),
        ("checks", ["pytest"], ".checks[0] must be an object"),
        ("checks", [{"check_value": "pytest"}], ".check_type"),
        ("checks", [{"check_type": []}], ".check_type"),
        ("checks", [{"check_type": "command"}], ".check_value"),
        ("checks", [{"check_type": "command", "check_value": " "}], ".check_value"),
        ("checks", [{"check_type": "command", "check_value": {}}], ".check_value"),
        ("checks", [{"check_type": "command", "check_value": 1}], ".check_value"),
        (
            "checks",
            [{"check_type": "command", "check_value": {"cmd": 1}}],
            ".check_value",
        ),
    ],
)
def test_item_shape_refused_claim_contract(
    monkeypatch: pytest.MonkeyPatch, field: str, value: object, reason: str
) -> None:
    monkeypatch.setenv(FLAG, "on")
    item = _item()
    item[field] = value
    # A valid preceding item cannot hide a malformed later one.
    assert reason in _reason(PLAIN_CLAIM + _contract([_item(), item]))


@pytest.mark.parametrize(
    "kind", ["disposition", "file_exists", "test_exists", "grep", "semantic_grading"]
)
def test_static_only_refused_claim_contract(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setenv(FLAG, "on")
    assert "dod-evidence= needs a behavior-proving item" in _reason(
        PLAIN_CLAIM + _contract([_item(kind, "tests/x.py")])
    )


@pytest.mark.parametrize(
    "command",
    [
        "gh pr view 1 --json state",
        "curl https://example.test/health",
        "grep test_passes tests/x.py",
        "env -u PYTHONPATH uv run gh pr view 1 --json state",
        "cd repo && gh pr view 1 --json state",
        "gh pr view 1 --json state && pytest",
        "echo pytest",
        "pytest-not-a-runner",
        "env --unknown pytest",
        "env -u",
        "env -S 'pytest'",
        "cd repo; pytest",
        "uv run --directory pytest gh pr view 1",
        "pytest 'unterminated",
        "",
    ],
)
def test_readback_and_unknown_commands_refused_claim_contract(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    monkeypatch.setenv(FLAG, "on")
    reason = _reason(PLAIN_CLAIM + _contract([_item(value=command)]))
    assert "dod-evidence" in reason
    assert "behavior-proving" in reason or "check_value" in reason


@pytest.mark.parametrize("kind", ["command", "command_exit_0"])
@pytest.mark.parametrize(
    "command",
    [
        "pytest tests/x.py -q",
        "uv run pytest tests/x.py -q",
        "env -u PYTHONPATH uv run pytest",
        "NAME=value SECOND='two words' pytest",
        "env -i --unset=PYTHONPATH NAME=value uv run -- pytest",
        "env --chdir repo -- pytest",
        "cd repo && cd subdir&&NAME=value uv run pytest",
        "py.test tests/x.py",
        "tox",
        "nox",
        "vitest run",
        "jest",
        "onex dod verify",
        "pytest tests/x.py ¦ cat",
    ],
)
def test_runner_commands_accepted_claim_contract(
    monkeypatch: pytest.MonkeyPatch, kind: str, command: str
) -> None:
    monkeypatch.setenv(FLAG, "on")
    assert g.row_refusals(PLAIN_CLAIM + _contract([_item(kind, command)])) == []


@pytest.mark.parametrize(
    "kind",
    [
        "test_passes",
        "behavior_proven",
        "endpoint",
        "rendered_output",
        "runtime_sha_match",
    ],
)
def test_behavior_check_types_accepted_claim_contract(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setenv(FLAG, "on")
    assert g.row_refusals(PLAIN_CLAIM + _contract([_item(kind, "proof target")])) == []


def test_mapping_value_shape_claim_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FLAG, "on")
    assert (
        g.row_refusals(
            PLAIN_CLAIM
            + _contract([_item("endpoint", {"url": "https://example.test"})])
        )
        == []
    )
    # A mapping is not a command string; do not infer execution from it.
    assert "behavior-proving" in _reason(
        PLAIN_CLAIM + _contract([_item("command", {"command": "pytest"})])
    )


def test_empty_checks_refused_claim_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FLAG, "on")
    item = _item()
    item["checks"] = []
    assert "behavior-proving" in _reason(PLAIN_CLAIM + _contract([item]))


@pytest.mark.parametrize("flag", [None, "0", "false", ""])
def test_flag_off_unchanged_claim_contract(
    monkeypatch: pytest.MonkeyPatch, flag: str | None
) -> None:
    if flag is None:
        monkeypatch.delenv(FLAG, raising=False)
    else:
        monkeypatch.setenv(FLAG, flag)

    def unexpected_validation(_fields: dict[str, list[str]]) -> str | None:
        pytest.fail("flag OFF must never inspect or parse the contract")

    monkeypatch.setattr(claim_contract, "claim_refusal", unexpected_validation)
    for cells in (
        "",
        " | dod-evidence={broken",
        _contract([_item("disposition", "superseded")]),
    ):
        assert g.row_refusals(PLAIN_CLAIM + cells) == []


def test_non_claim_unchanged_claim_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FLAG, "on")
    assert g.row_refusals(f"{STAMP} | STATUS | lane=lane-a | dod-evidence=bad") == []


def test_payload_checks_claim_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FLAG, "on")
    refused = g.refusal_for_payload(PLAIN_CLAIM, "ROLLING_WORK_LEDGER.md")
    assert refused is not None
    assert "OMN-20029" in refused
    valid = PLAIN_CLAIM + _contract([_item()])
    assert g.refusal_for_payload(valid, "ROLLING_WORK_LEDGER.md") is None
