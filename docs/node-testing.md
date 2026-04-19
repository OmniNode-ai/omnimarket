# Node Testing Pattern

Testing harness for skill-to-node dispatch parity validation [OMN-8008].

## Overview

Every ported node must satisfy three properties:

1. **Standalone execution**: `python -m omnimarket.nodes.<node_name> --dry-run` exits 0 or 1 and writes valid JSON to stdout.
2. **Schema parity**: the JSON output is parseable as the node's result model (e.g. `CoverageSweepResult`).
3. **Handler parity**: direct `handler.handle(request)` and subprocess invocation produce structurally equivalent output.

## Test file

`tests/test_skill_dispatch.py` — parametrized over Wave 1 ported nodes:

- `node_coverage_sweep`
- `node_runtime_sweep`
- `node_aislop_sweep`

All tests are marked `@pytest.mark.unit` and run without network or database access.

## Running

```bash
uv run pytest tests/test_skill_dispatch.py -v -m unit
```

## Adding a new node

When porting a new skill to a node, add it to the parameterized list in `test_node_dry_run_exits_and_writes_json` and add a dedicated parity test following the pattern of `test_coverage_sweep_parity`.

Requirements for a node to be harness-compatible:

1. Has a `__main__.py` with a `--dry-run` flag that exits 0 when there are no findings, 1 when findings exist.
2. Writes `result.model_dump_json(indent=2)` to stdout.
3. The result model has a `status` or `findings` field (or both).

## CI gate

The harness runs as part of the standard `pytest -m unit` suite in CI. No separate configuration required.
