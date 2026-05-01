# Skill: ticket_pipeline

| Field | Value |
| --- | --- |
| Skill | ticket_pipeline |
| Node | node_ticket_pipeline |
| Contract | ticket_pipeline v1.0.0 |
| Mode | dry_run |
| Status | blocked |
| Run ID | 11111111-1111-4111-8111-111111111111 |

## Execution

| Step | Status | Description |
| --- | --- | --- |
| pre_flight | succeeded | validated command envelope |
| local_review | blocked | implementation pending |

## Evidence

- contract: src/omnimarket/nodes/node_ticket_pipeline/contract.yaml - terminal event source

## Result

- stop_reason: not_implemented
- terminal_event: onex.evt.omnimarket.ticket-pipeline-completed.v1
