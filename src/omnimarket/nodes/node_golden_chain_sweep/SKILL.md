# golden_chain_sweep

Validates field-level correctness of **pre-collected** projection rows against
chain definitions.

> **Evidence scope.** This node is **pure compute over
> caller-supplied `projected_rows`** — it performs **zero live I/O**: no Kafka
> publish, no DB poll, no `count(*)`, no row-count delta. A `pass` means only
> that the rows the caller handed in contain the expected field keys. It does
> **NOT** prove that any event flowed end-to-end or that any row materialized in
> a live tail table.
>
> **Do NOT cite a `golden_chain_sweep` pass as live row / end-to-end data-flow
> evidence.** It is a deterministic field-presence validator, suitable for
> regression/diagnostic checks over rows you have already fetched by other means.
> A real live-Postgres fetch + row-count-delta assertion (publish a head event,
> read the tail-table row back, assert the delta) is tracked as a follow-on task
> and is **not** implemented here yet.

## Chain Registry

Chains are defined in `golden_chains.yaml` (co-located with this node).
The sweep tool reads this file at startup — adding a new chain here automatically
includes it in sweep coverage without touching any Python code.

**Registry schema** (each entry under `chains:`):

| Field | Required | Description |
|---|---|---|
| `name` | yes | Unique chain identifier, used as key in `--chains` filter |
| `head_topic` | yes | Kafka topic that initiates the chain |
| `tail_table` | yes | DB projection table that should receive the event |
| `expected_fields` | no | Fields that must be present in the projected row |

**Example:**

```yaml
chains:
  - name: registration
    head_topic: onex.evt.omniclaude.routing-decision.v1
    tail_table: agent_routing_decisions
    expected_fields:
      - correlation_id
      - selected_agent
```

If `golden_chains.yaml` is missing or unreadable, the node falls back to an
empty chain list and logs a warning.

## Usage

```bash
# Run all chains from registry
python -m omnimarket.nodes.node_golden_chain_sweep

# Run specific chains
python -m omnimarket.nodes.node_golden_chain_sweep --chains registration,routing

# Pass pre-collected projection data
python -m omnimarket.nodes.node_golden_chain_sweep \
    --projected-rows '{"registration": {"correlation_id": "abc", "selected_agent": "x"}}'
```

## Output

JSON `GoldenChainSweepResult` to stdout. Exit code 0 on overall pass, 1 otherwise.
