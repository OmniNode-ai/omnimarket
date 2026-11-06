# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the ticket contract merge (OMN-19998).

The fixture is the shape omnimarket#5078 and #5103 merged in: two sides append entries whose last
check ends in the line ``cwd: .``, after a base whose last line is the same.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract
from omnibase_core.utils.util_safe_yaml_loader import load_yaml_mapping_no_duplicates
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_subscribe_topics,
)
from omnimarket.nodes.node_ticket_contract_merge_compute import (
    NodeTicketContractMergeCompute,
)
from omnimarket.nodes.node_ticket_contract_merge_compute import __main__ as driver
from omnimarket.nodes.node_ticket_contract_merge_compute.handlers.handler_ticket_contract_merge import (
    HandlerTicketContractMerge,
)
from omnimarket.nodes.node_ticket_contract_merge_compute.models import (
    EnumTicketContractMergeOutcome,
    EnumTicketContractMergeRefusal,
    ModelTicketContractMergeRequest,
    ModelTicketContractMergeResult,
)

pytestmark = pytest.mark.unit

CONTRACT = Path(driver.__file__).parent / "contract.yaml"
HEAD = """schema_version: 1.0.0
ticket_id: OMN-19998
title: Shared contract
summary: Every pull request appends here.
is_seam_ticket: false
interface_change: true
interfaces_touched: [events]
requirements:
  - id: req-base
    statement: The base requirement.
    acceptance:
      - id: AC1
        statement: 'AC1: the base criterion -- falsifier: uv run pytest tests/base.py -q'
evidence_requirements: []
emergency_bypass:
  enabled: false
  justification: ''
  follow_up_ticket_id: ''
dod_evidence:
  - id: dod-base
    description: The base entry.
    source: manual
    binds_ac: [AC1]
    checks:
      - check_type: test_passes
        check_value: uv run pytest tests/base.py -q
        cwd: .
"""


def entry(name: str, label: str) -> str:
    return f"""  - id: dod-{name}
    description: Entry of {name}.
    source: manual
    binds_ac: [{label}]
    checks:
      - check_type: test_passes
        check_value: uv run pytest tests/{name}.py -q
        cwd: .
"""


def requirement(name: str, label: str) -> str:
    return f"""  - id: req-{name}
    statement: Requirement of {name}.
    acceptance:
      - id: {label}
        statement: '{label}: {name} holds -- falsifier: uv run pytest tests/{name}.py -q'
"""


def side(*names: tuple[str, str]) -> str:
    text = HEAD
    marker = "evidence_requirements: []\n"
    for name, label in names:
        text = text.replace(marker, requirement(name, label) + marker) + entry(
            name, label
        )
    return text


def merged(base: str, current: str, incoming: str) -> ModelTicketContractMergeResult:
    return HandlerTicketContractMerge().handle(
        ModelTicketContractMergeRequest(
            base_text=base, current_text=current, incoming_text=incoming
        )
    )


def refusal_codes(result: ModelTicketContractMergeResult) -> list[str]:
    return [refusal.code.value for refusal in result.refusals]


def test_golden_chain_real_shape_keeps_every_entry_whole() -> None:
    result = merged(HEAD, side(("pr", "AC2")), side(("dev", "AC3")))

    assert result.outcome is EnumTicketContractMergeOutcome.MERGED
    parsed = load_yaml_mapping_no_duplicates(result.merged_text, source="merged")
    ModelTicketContract.from_yaml(result.merged_text)
    by_id = {item["id"]: item for item in parsed["dod_evidence"]}
    assert list(by_id) == ["dod-base", "dod-pr", "dod-dev"]
    assert [item["checks"][0].get("cwd") for item in by_id.values()] == [".", ".", "."]
    assert [item["id"] for item in parsed["requirements"]] == [
        "req-base",
        "req-pr",
        "req-dev",
    ]
    assert result.merged_text.startswith(side(("pr", "AC2")).split("  - id: req-pr")[0])
    assert "dod_evidence:dod-dev" in result.entries_from_incoming


def test_named_break_a_line_union_of_the_same_three_texts_loses_the_line(
    tmp_path: Path,
) -> None:
    """The failing control: git's own union merge, which this node replaces, drops ``cwd``."""
    (tmp_path / "base").write_text(HEAD)
    (tmp_path / "current").write_text(side(("pr", "AC2")))
    (tmp_path / "incoming").write_text(side(("dev", "AC3")))
    union = subprocess.run(
        [
            "git",
            "merge-file",
            "-p",
            "--union",
            str(tmp_path / "current"),
            str(tmp_path / "base"),
            str(tmp_path / "incoming"),
        ],
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    items = {item["id"]: item for item in yaml.safe_load(union)["dod_evidence"]}
    assert "cwd" not in items["dod-pr"]["checks"][0]


def test_flow_lists_and_empty_lists_merge_by_value() -> None:
    current = HEAD.replace("[events]", "[events, public_api]")
    incoming = HEAD.replace("[events]", "[events, topics]").replace(
        "evidence_requirements: []\n",
        "evidence_requirements:\n  - kind: tests\n    description: The tests.\n"
        "    command: uv run pytest tests/x.py -q\n",
    )
    result = merged(HEAD, current, incoming)

    assert result.outcome is EnumTicketContractMergeOutcome.MERGED, result.refusals
    parsed = yaml.safe_load(result.merged_text)
    assert parsed["interfaces_touched"] == ["events", "public_api", "topics"]
    assert [item["kind"] for item in parsed["evidence_requirements"]] == ["tests"]


def test_one_side_changing_an_entry_or_field_takes_that_side() -> None:
    incoming = HEAD.replace("The base entry.", "The changed entry.").replace(
        "Every pull request appends here.", "A changed summary."
    )
    result = merged(HEAD, side(("pr", "AC2")), incoming)

    assert result.outcome is EnumTicketContractMergeOutcome.MERGED, result.refusals
    parsed = yaml.safe_load(result.merged_text)
    assert parsed["summary"] == "A changed summary."
    assert parsed["dod_evidence"][0]["description"] == "The changed entry."
    assert parsed["dod_evidence"][1]["id"] == "dod-pr"


def test_the_same_entry_added_identically_on_both_sides_appears_once() -> None:
    result = merged(HEAD, side(("both", "AC2")), side(("both", "AC2")))

    assert result.outcome is EnumTicketContractMergeOutcome.MERGED
    ids = [item["id"] for item in yaml.safe_load(result.merged_text)["dod_evidence"]]
    assert ids == ["dod-base", "dod-both"]


def test_error_chain_injected_duplicate_id_within_a_side_is_refused() -> None:
    duplicated = side(("pr", "AC2")) + entry("pr", "AC2")
    result = merged(HEAD, duplicated, side(("dev", "AC3")))

    assert result.outcome is EnumTicketContractMergeOutcome.REFUSED
    assert result.merged_text == ""
    assert result.refusals[0].code is EnumTicketContractMergeRefusal.DUPLICATE_ID
    assert result.refusals[0].entry == "dod-pr"


def test_error_chain_one_id_added_differently_on_both_sides_is_refused() -> None:
    other = side(("pr", "AC2")).replace("Entry of pr.", "A different entry of pr.")
    result = merged(HEAD, side(("pr", "AC2")), other)

    assert refusal_codes(result) == ["divergent_entry"]
    assert result.refusals[0].entry == "dod-pr"


def test_error_chain_a_field_changed_differently_on_both_sides_is_refused() -> None:
    result = merged(
        HEAD,
        HEAD.replace("Shared contract", "One title"),
        HEAD.replace("Shared contract", "Another title"),
    )

    assert refusal_codes(result) == ["divergent_field"]


def test_error_chain_removed_on_one_side_and_changed_on_the_other_is_refused() -> None:
    with_entry = side(("pr", "AC2"))
    changed = with_entry.replace("Entry of pr.", "Changed entry of pr.")
    result = merged(with_entry, HEAD, changed)

    assert "removed_and_changed" in refusal_codes(result)


def test_error_chain_interleaved_or_duplicate_key_text_is_refused() -> None:
    assert refusal_codes(merged(HEAD, "a: [", HEAD)) == ["unparseable"]
    duplicate_key = HEAD + "title: Second\n"
    assert refusal_codes(merged(HEAD, duplicate_key, HEAD)) == ["unparseable"]


def test_error_chain_a_merge_that_breaks_the_contract_model_is_refused() -> None:
    broken = HEAD.replace("ticket_id: OMN-19998\n", "")
    result = merged(HEAD, broken, broken)

    assert refusal_codes(result) == ["invalid_contract"]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        env=scrub_git_location_env(
            {
                **os.environ,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@example.invalid",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@example.invalid",
                "PYTHONPATH": os.pathsep.join(path for path in sys.path if path),
            }
        ),
    )


def _repository(tmp_path: Path, current: str, incoming: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    contract = repo / "contracts" / "OMN-19998.yaml"
    contract.parent.mkdir()
    (repo / ".gitattributes").write_text("contracts/OMN-*.yaml merge=ticket-contract\n")
    _git(repo, "init", "-q", "-b", "dev")
    _git(
        repo,
        "config",
        "merge.ticket-contract.driver",
        f"{sys.executable} -m omnimarket.nodes.node_ticket_contract_merge_compute %O %A %B %P",
    )
    contract.write_text(HEAD)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "pr")
    contract.write_text(current)
    _git(repo, "commit", "-qam", "pr")
    _git(repo, "checkout", "-q", "dev")
    contract.write_text(incoming)
    _git(repo, "commit", "-qam", "dev")
    _git(repo, "checkout", "-q", "pr")
    return repo


def test_golden_chain_git_merge_uses_the_declared_driver(tmp_path: Path) -> None:
    repo = _repository(tmp_path, side(("pr", "AC2")), side(("dev", "AC3")))

    done = _git(repo, "merge", "--no-edit", "dev")

    assert done.returncode == 0, done.stdout + done.stderr
    parsed = yaml.safe_load((repo / "contracts" / "OMN-19998.yaml").read_text())
    assert all(item["checks"][0]["cwd"] == "." for item in parsed["dod_evidence"])


def test_error_chain_git_merge_reads_a_refusal_as_a_conflict(tmp_path: Path) -> None:
    repo = _repository(
        tmp_path, side(("pr", "AC2")) + entry("pr", "AC2"), side(("dev", "AC3"))
    )

    done = _git(repo, "merge", "--no-edit", "dev")

    assert done.returncode != 0
    assert "duplicate_id" in done.stdout + done.stderr


def test_golden_chain_event_over_the_declared_topics() -> None:
    """A command on the declared topic reaches the handler; the terminal is on the declared topic."""
    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

    from tests.runtime_local_compat import LocalRuntimeBusAdapter

    class _AsyncHandler:
        async def handle(
            self, request: ModelTicketContractMergeRequest
        ) -> ModelTicketContractMergeResult:
            return NodeTicketContractMergeCompute().handle(request)

    async def run(request: dict[str, str]) -> dict[str, object]:
        bus = EventBusInmemory(environment="merge-proof", group="merge-proof")
        await bus.start()
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        command = contract_subscribe_topics(CONTRACT)[0]
        terminal = contract_publish_topics(CONTRACT)[0]

        async def observe(message: object) -> None:
            await queue.put(json.loads(message.value))  # type: ignore[attr-defined]

        adapter = LocalRuntimeBusAdapter(
            handler=_AsyncHandler(),
            handler_name="HandlerTicketContractMerge",
            input_model_cls=ModelTicketContractMergeRequest,
            output_topic=terminal,
            bus=bus,
            on_error=lambda: None,
        )
        stops = [
            await bus.subscribe_ungrouped(terminal, observe),
            await bus.subscribe_ungrouped(command, adapter.on_message),
        ]
        await bus.publish(command, None, json.dumps(request).encode())
        answer = await asyncio.wait_for(queue.get(), timeout=10)
        for stop in stops:
            await stop()
        await bus.close()
        return answer

    good = asyncio.run(
        run(
            {
                "base_text": HEAD,
                "current_text": side(("pr", "AC2")),
                "incoming_text": side(("dev", "AC3")),
            }
        )
    )
    assert good["outcome"] == "merged"

    bad = asyncio.run(
        run(
            {
                "base_text": HEAD,
                "current_text": side(("pr", "AC2")) + entry("pr", "AC2"),
                "incoming_text": side(("dev", "AC3")),
            }
        )
    )
    assert bad["outcome"] == "refused"
    assert bad["refusals"][0]["code"] == "duplicate_id"  # type: ignore[index]
