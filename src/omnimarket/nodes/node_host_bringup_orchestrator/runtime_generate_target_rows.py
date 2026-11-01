# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Write the orchestrator contract's target rows from the plan text (OMN-21883).

The plan's graph section (section 5) holds one table row per target. This
module reads those rows, so the name, stage, needs and falsifier of each
contract row are the plan's own words and are not retyped. The two things the
plan states only in prose, the kind of the already-satisfied check and the
action, are classified into the closed enums by ``_CLASSIFICATION``. A plan row
the table does not name is refused, and so is a classification the row's action
words contradict (a ``person`` action must be the person step, ``stop with``
the stop, ``none`` no action).

    uv run python -m omnimarket.nodes.node_host_bringup_orchestrator.runtime_generate_target_rows --plan <plan file> --write
    uv run python -m omnimarket.nodes.node_host_bringup_orchestrator.runtime_generate_target_rows --plan <plan file> --check

``--check`` exits 1 when the committed contract differs from what the plan
yields. The tests carry no copy of the plan: they bind the committed rows and
run this module on a plan excerpt.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

from omnimarket.nodes.node_host_bringup_orchestrator.models import ModelPlanTargetRow
from omnimarket.nodes.node_host_bringup_plan_compute.models import (
    EnumBringupAction,
    EnumBringupCheckKind,
)

_CONTRACT = Path(__file__).resolve().parent / "contract.yaml"

_EXPECTED_STAGE_COUNTS = {"A": 4, "B": 7, "C": 15, "D": 8, "E": 7, "F": 9, "G": 7}
_EXPECTED_TOTAL = 57
_TOP_TARGETS = ("up", "storage-up")

_GRAPH_HEADING = re.compile(r"^## 5\.\s")
_NEXT_HEADING = re.compile(r"^## \d+\.\s")
_STAGE_HEADING = re.compile(r"^### Stage ([A-Z]):")
_CELLS = 9

C = EnumBringupCheckKind
A = EnumBringupAction

# name -> (check kind, action). The plan states neither as an enum member.
_CLASSIFICATION: dict[str, tuple[EnumBringupCheckKind, EnumBringupAction]] = {
    "card": (C.DECLARATION_VALID, A.STOP),
    "ssh-config": (C.TRANSPORT_ANSWERS, A.LOCAL_INCLUDE),
    "declared": (C.DECLARATION_DRIFT, A.DATA_PR),
    "adopted": (C.REVISION_CURRENT, A.ADOPT_MERGED),
    "reservations": (C.PERSON_CONFIRMED, A.PERSON_STEP),
    "ilo-access": (C.MANAGEMENT_READ, A.PERSON_STEP),
    "firmware-settings": (C.MANAGEMENT_READ, A.MANAGEMENT_WRITE),
    "volumes": (C.MANAGEMENT_READ, A.PERSON_STEP),
    "os-installed": (C.TRANSPORT_ANSWERS, A.PERSON_STEP),
    "host-key": (C.PERSON_CONFIRMED, A.PERSON_STEP),
    "key-login": (C.TRANSPORT_ANSWERS, A.PERSON_STEP),
    "root-access": (C.TRANSPORT_ANSWERS, A.PERSON_STEP),
    "machine-identity": (C.HOST_STATE, A.STOP),
    "hostname": (C.HOST_STATE, A.LINE_IN_FILE),
    "apt-sources": (C.FILE_STATE, A.APT_SOURCE),
    "data-layout": (C.FILE_STATE, A.DIRECTORY),
    "docker-config": (C.FILE_STATE, A.FILE_CONTENT),
    "packages": (C.PACKAGE_INSTALLED, A.APT_PACKAGES),
    "docker-running": (C.SERVICE_ACTIVE, A.SERVICE_ENABLED),
    "account-grants": (C.GROUP_MEMBER, A.GROUP_MEMBER),
    "system-services": (C.SERVICE_ACTIVE, A.SERVICE_ENABLED),
    "memory-guard": (C.FILE_STATE, A.FILE_CONTENT),
    "bwrap-userns": (C.HOST_STATE, A.FILE_CONTENT),
    "launcher-key": (C.FILE_STATE, A.LINE_IN_FILE),
    "tailnet": (C.HOST_STATE, A.TAILNET_LOGIN),
    "sshd-key-only": (C.HOST_STATE, A.GUARDED_SSHD_DROPIN),
    "lab-dirs": (C.FILE_STATE, A.DIRECTORY),
    "user-tools": (C.PACKAGE_INSTALLED, A.TOOL_INSTALL),
    "clones": (C.CHECKOUT_STATE, A.CLONE_REPOSITORY),
    "venvs": (C.CHECKOUT_STATE, A.SYNC_ENVIRONMENT),
    "plugin-tree": (C.CHECKOUT_STATE, A.INSTALL_PLUGIN_TREE),
    "lane-guard": (C.UNIT_STATUS, A.HOST_UNIT_INSTALL),
    "lane-account": (C.UNIT_STATUS, A.HOST_UNIT_INSTALL),
    "lane-env": (C.FILE_STATE, A.FILE_CONTENT),
    "store-identity": (C.CREDENTIAL_RESOLVES, A.STORE_CREDENTIAL),
    "bus-logins": (C.CREDENTIAL_RESOLVES, A.MINT_LOGIN),
    "broker-grants": (C.CREDENTIAL_RESOLVES, A.APPLY_BROKER_GRANTS),
    "claude-pool": (C.CREDENTIAL_RESOLVES, A.REFILL_CLAUDE_POOL),
    "codex-login": (C.CREDENTIAL_RESOLVES, A.STORE_CREDENTIAL),
    "github-login": (C.CREDENTIAL_RESOLVES, A.STORE_CREDENTIAL),
    "delegation-key": (C.CREDENTIAL_RESOLVES, A.STORE_CREDENTIAL),
    "facts-declared": (C.DECLARATION_DRIFT, A.DATA_PR),
    "ram-filesystem": (C.UNIT_STATUS, A.HOST_UNIT_INSTALL),
    "host-units": (C.UNIT_STATUS, A.HOST_UNIT_INSTALL),
    "collaborator-accounts": (C.HOST_STATE, A.ROLE_ACTION),
    "load-measured": (C.MEASUREMENT_CURRENT, A.RUN_WORKLOAD),
    "lab-work-serve": (C.UNIT_STATUS, A.HOST_UNIT_INSTALL),
    "verify": (C.RUNBOOK_VERIFIED, A.STOP),
    "in-service": (C.TABLE_ROW, A.DATA_PR),
    "up": (C.AGGREGATE, A.NO_ACTION),
    "nvme-fitted": (C.HOST_STATE, A.PERSON_STEP),
    "data-volume": (C.HOST_STATE, A.PREPARE_VOLUME),
    "data-moved": (C.HOST_STATE, A.MOVE_DATA_ROOTS),
    "storage-verified": (C.MEASUREMENT_CURRENT, A.RERUN_TARGETS),
    "cap-raised": (C.MEASUREMENT_CURRENT, A.DATA_PR),
    "ci-capacity": (C.FLEET_HEALTHY, A.DEPLOY_RUNNERS),
    "storage-up": (C.AGGREGATE, A.NO_ACTION),
}


class PlanShapeError(ValueError):
    """The plan text does not hold the graph the contract is generated from."""


def _graph_section(plan_text: str) -> list[str]:
    lines = plan_text.splitlines()
    start = next((i for i, ln in enumerate(lines) if _GRAPH_HEADING.match(ln)), None)
    if start is None:
        raise PlanShapeError("the plan has no graph section (a heading '## 5.')")
    end = next(
        (i for i in range(start + 1, len(lines)) if _NEXT_HEADING.match(lines[i])),
        len(lines),
    )
    return lines[start + 1 : end]


def _name(cell: str) -> str:
    return cell.strip().strip("`").strip()


def parse_plan_rows(plan_text: str) -> list[ModelPlanTargetRow]:
    """The table rows of the plan's graph section, in plan order."""
    rows: list[ModelPlanTargetRow] = []
    stage: str | None = None
    for line in _graph_section(plan_text):
        heading = _STAGE_HEADING.match(line)
        if heading:
            stage = heading.group(1)
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not (line.lstrip().startswith("|") and cells[0].isdigit()):
            continue
        label = f"row {cells[0]} ({_name(cells[1]) if len(cells) > 1 else '?'})"
        if stage is None:
            raise PlanShapeError(f"{label}: no stage heading before it")
        if len(cells) != _CELLS or not all(cells[:7]):
            raise PlanShapeError(
                f"{label}: expected {_CELLS} cells, none empty before the ticket"
            )
        needs = (
            ()
            if cells[2].lower() == "none"
            else tuple(n.strip() for n in cells[2].split(","))
        )
        rows.append(
            ModelPlanTargetRow(
                number=int(cells[0]),
                name=_name(cells[1]),
                stage=stage,
                needs=needs,
                check=cells[3],
                action=cells[4],
                falsifier=cells[5],
            )
        )
    if not rows:
        raise PlanShapeError("the plan's graph section holds no target rows")
    return rows


def _refuse_contradiction(row: ModelPlanTargetRow, action: EnumBringupAction) -> None:
    """Refuse a classification the plan's own action words contradict."""
    words = row.action.lower().lstrip("`")
    claims = (
        (words.startswith("person"), A.PERSON_STEP),
        (words == "none", A.NO_ACTION),
        (words.startswith(("stop with", "stop on", "stop naming")), A.STOP),
    )
    for said, expected in claims:
        if said != (action is expected):
            raise PlanShapeError(
                f"{row.name}: the plan's action words '{row.action}' contradict "
                f"the classification {action.value}"
            )


def _refuse_wrong_shape(rows: list[ModelPlanTargetRow]) -> None:
    numbers = [r.number for r in rows]
    if numbers != list(range(1, len(rows) + 1)):
        raise PlanShapeError(f"row numbers are not 1..{len(rows)} in order: {numbers}")
    counts = dict(sorted(Counter(r.stage for r in rows).items()))
    if len(rows) != _EXPECTED_TOTAL or counts != _EXPECTED_STAGE_COUNTS:
        raise PlanShapeError(
            f"expected {_EXPECTED_TOTAL} targets as {_EXPECTED_STAGE_COUNTS}, "
            f"the plan has {len(rows)} as {counts}"
        )
    missing = sorted(set(_CLASSIFICATION) - {r.name for r in rows})
    if missing:
        raise PlanShapeError(f"classified targets the plan no longer lists: {missing}")


_HEADER = """\
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# ONEX Node Contract - Host Bring-up Orchestrator target graph (OMN-21883)
#
# The 57 declared bring-up targets of the host bring-up dependency graph plan,
# stages A to G (4, 7, 15, 8, 7, 9 and 7). Each row holds its stage, its needs, a
# read-only already-satisfied check, a falsifier and an enum action, and loads
# through load_bringup_graph in node_host_bringup_plan_compute; a row missing any
# of them fails the load. The rows hold shape and no value: a check subject is
# a symbolic identifier, and no host, address, path or version appears here.
#
# GENERATED. Do not edit by hand. Name, stage, needs and falsifier are read from
# the plan's graph section; the check kind and action are classified by the
# generator. Rewrite with:
#   uv run python -m omnimarket.nodes.node_host_bringup_orchestrator.runtime_generate_target_rows --plan <plan file> --write
# Top targets: up (stages A to F) and storage-up (stage G, which needs up).
name: "node_host_bringup_orchestrator"
node_type: "ORCHESTRATOR_GENERIC"
contract_version:
  major: 0
  minor: 1
  patch: 0
node_version: "0.1.0"
lifecycle: experimental
description: "The declared host bring-up target graph: 57 targets, each with a check, a falsifier and an enum action."
input_model:
  name: "ModelHostBringupPlanRequest"
  module: "omnimarket.nodes.node_host_bringup_plan_compute.models.model_host_bringup_plan"
  description: "The declared target rows and each target's observed check result."
output_model:
  name: "ModelHostBringupPlan"
  module: "omnimarket.nodes.node_host_bringup_plan_compute.models.model_host_bringup_plan"
  description: "Planned steps in need order with skip or act per target, or the refusals."
handler:
  module: "omnimarket.nodes.node_host_bringup_plan_compute.handlers.handler_host_bringup_plan"
  class: "HandlerHostBringupPlan"
  input_model: "omnimarket.nodes.node_host_bringup_plan_compute.models.model_host_bringup_plan.ModelHostBringupPlanRequest"
  output_model: "omnimarket.nodes.node_host_bringup_plan_compute.models.model_host_bringup_plan.ModelHostBringupPlan"
handler_routing:
  routing_strategy: "operation_match"
  default_handler: "omnimarket.nodes.node_host_bringup_plan_compute.handlers.handler_host_bringup_plan:HandlerHostBringupPlan"
  handlers:
    - operation: "plan_host_bringup"
      handler:
        name: "HandlerHostBringupPlan"
        module: "omnimarket.nodes.node_host_bringup_plan_compute.handlers.handler_host_bringup_plan"
descriptor:
  node_archetype: orchestrator
  purity: pure
  idempotent: true
  timeout_ms: 5000
dod_evidence:
  PYTEST:
    description: "the 57 rows load, count 4, 7, 15, 8, 7, 9 and 7 per stage, and a planted row without a falsifier is refused"
    command: "uv run pytest tests/unit/nodes/test_node_host_bringup_orchestrator_rows.py -q"
    pass_criteria: "every test passes"
targets:
"""


def build_contract_text(
    rows: list[ModelPlanTargetRow], *, expect_shape: bool = True
) -> str:
    """The contract file text for the plan rows: the header, then one block per target."""
    if expect_shape:
        _refuse_wrong_shape(rows)
    blocks: list[str] = []
    for row in rows:
        if row.name not in _CLASSIFICATION:
            raise PlanShapeError(
                f"{row.name}: no classification (check kind and action) for this target"
            )
        kind, action = _CLASSIFICATION[row.name]
        _refuse_contradiction(row, action)
        lines = [f"  - name: {row.name}", f"    stage: {row.stage}"]
        if row.name in _TOP_TARGETS:
            lines.append("    top: true")
        lines += [
            f"    needs: [{', '.join(row.needs)}]",
            f"    check: {{kind: {kind.value}, subject: {row.name.replace('-', '_')}}}",
            f"    falsifier: {json.dumps(row.falsifier, ensure_ascii=False)}",
            f"    action: {action.value}",
        ]
        blocks.append("\n".join(lines))
    return _HEADER + "\n".join(blocks) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the bring-up target rows.")
    parser.add_argument("--plan", type=Path, required=True, help="the plan text file")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="rewrite the contract")
    mode.add_argument("--check", action="store_true", help="exit 1 on any drift")
    args = parser.parse_args(argv)

    text = build_contract_text(parse_plan_rows(args.plan.read_text(encoding="utf-8")))
    if args.write:
        _CONTRACT.write_text(text, encoding="utf-8")
        sys.stdout.write(f"wrote {_EXPECTED_TOTAL} targets\n")
        return 0
    if _CONTRACT.read_text(encoding="utf-8") != text:
        sys.stderr.write("the committed contract differs from the plan's rows\n")
        return 1
    sys.stdout.write("the committed contract matches the plan's rows\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
