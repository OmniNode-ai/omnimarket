# Contributing

OmniMarket changes should preserve the contract-first package boundary.

## Setup

```bash
uv sync --all-extras
```

## Before Opening A PR

Run the checks that match the change:

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/omnimarket/ --strict
uv run pytest tests/ -v --tb=short -m "not kafka"
uv run python scripts/ci/run_runtime_sweep.py
uv run python scripts/ci/check_node_metadata_dependencies.py
```

For node changes, also run the focused golden-chain or contract test for the
node you touched.

## Node Changes

- Keep event topics in `contract.yaml`.
- Keep dependency and capability declarations in `metadata.yaml`.
- Add or update a golden-chain test.
- Do not make one node import another node's private handler or model package.
  Promote shared types into a shared Market package instead.

## Documentation Changes

- Root `README.md` is the human entrypoint.
- `docs/README.md` is the docs map.
- Current architecture belongs under `docs/architecture/`.
- Durable migration context belongs under `docs/migrations/`.
- Historical tracking, evidence notes, and one-off operational records should
  not be added as public docs.
