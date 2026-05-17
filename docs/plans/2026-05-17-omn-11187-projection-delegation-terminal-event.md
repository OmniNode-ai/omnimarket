# OMN-11187: Fix projection reducer nodes must emit terminal event on projection-delegation-applied topic

## Problem

`DelegationProjectionRunner` writes DB rows but never publishes to
`onex.evt.omnimarket.projection-delegation-applied.v1`. Pattern B broker
consumers waiting for that terminal topic never receive confirmation, so
consumer lag never returns to 0.

## Approach

Add an optional `publish_fn` injectable to `DelegationProjectionRunner.__init__`.
After a successful `project_event()`, call `_emit_terminal_event()` which
publishes a `ModelEventEnvelope` JSON envelope to the topic declared as
`terminal_event` in `contract.yaml`.

In production the runner builds an `AIOKafkaProducer` lazily. In tests a
mock callable is injected.

## Fix location

`src/omnimarket/nodes/node_projection_delegation/handlers/handler_delegation.py`

## Test

`tests/test_golden_chain_projection_delegation.py` — add
`TestTerminalEventEmission` class that injects a mock publish_fn and asserts
the terminal topic receives an envelope with the correct correlation_id.
