# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A plan shaped like the floor's plans (header, tasks, labelled criteria, Lab step), written for the tests."""

from __future__ import annotations

from omnimarket.nodes.node_floor_task_ticket_payload_compute.models import (
    ModelFloorTaskTicketPayloadRequest,
)

LINE = "2026-11-02-example-plan"
PARENT = "OMN-70001"

HEADER = f"""# Example plan: one thing built in two tasks

- **state_as_of:** 2026-11-02T22:29:19Z
- **Goal:** build the example.
- **Ticket:** {PARENT} (the epic of this plan). The umbrella OMN-70000 is never a lane's ticket.

## 1. In plain words

Prose that names OMN-70002 and a Ticket: line inside a section that is not the header.
"""

GOOD_TASK = """### T1. The example node: `node_example_compute`

- **Files:** `src/example/node_example_compute/`.
- **Change:** a pure compute.
- **Test first:** a fixture.
- **Focused command:** `uv run pytest src/example -q`
- **Lab step:** run the node on the lab host with the recorded fixture and read its exit code.
- **Acceptance criteria:**
  1. The node returns the verdict for the fixture -- falsifier: `-k fixture_verdicts`.
  2. The same input twice is byte-identical -- falsifier: run twice, compare `model_dump_json`.
"""

NO_FALSIFIER_TASK = """### T2. A task whose second criterion names no check

- **Lab step:** run it on the lab host.
- **Acceptance criteria:**
  1. The first criterion -- falsifier: `-k first`.
  2. The second criterion names nothing that would settle it.
"""

NO_CRITERIA_TASK = """### T3. A task with no criteria

- **Lab step:** run it on the lab host.
- **Test first:** a fixture.
"""

NO_INTEGRATION_TASK = """### T4. A task whose Lab step is only none

- **Lab step:** none.
- **Acceptance criteria:**
  1. A criterion -- falsifier: `-k only`.
"""

NONE_BUT_NAMED_TASK = """### T5. A task whose Lab step is none but names the task that runs it live

- **Lab step:** none here; T9 runs it against the live record.
- **Acceptance criteria:**
  1. A criterion -- falsifier: `-k named`.
"""

WRAPPED_TASK = """### T6. A task whose criterion wraps onto a second line

- **Lab step:** run it on the lab host.
- **Acceptance criteria:**
  1. A criterion that wraps
     onto a second line -- falsifier: `-k wrapped`.
  2. A criterion in one line -- falsifier: `-k one`.
- **Acceptance note:** this field is not read.
"""

PLAN = (
    HEADER
    + "\n## 4. Tasks\n\n"
    + "\n".join(
        (
            GOOD_TASK,
            NO_FALSIFIER_TASK,
            NO_CRITERIA_TASK,
            NO_INTEGRATION_TASK,
            NONE_BUT_NAMED_TASK,
            WRAPPED_TASK,
        )
    )
    + "\n## 5. After the tasks\n\n- **Acceptance criteria:**\n  1. Not a task.\n"
)

PLAN_WITHOUT_TICKET = PLAN.replace(
    f"- **Ticket:** {PARENT} (the epic of this plan)",
    "- **Ticket:** none yet (plan before tickets)",
)


def request(
    task_id: str = "T1", plan_text: str | None = PLAN, line_id: str = LINE
) -> ModelFloorTaskTicketPayloadRequest:
    return ModelFloorTaskTicketPayloadRequest(
        line_id=line_id, task_id=task_id, plan_text=plan_text
    )
