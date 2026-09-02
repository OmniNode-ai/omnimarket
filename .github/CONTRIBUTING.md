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
uv run python -m omnimarket.nodes.node_runtime_sweep --import-check
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

- Root `README.md` is the human entrypoint and links out to the OmniNode
  knowledge base.
- Current architecture, guides, and reference material live in the public
  [OmniNode knowledge base](https://github.com/OmniNode-ai/knowledge-base)
  (`architecture/`, `guides/`, `reference/`), not in this repo. Governance,
  runbook, and operator-facing content that needs real infra values lives in
  [knowledge-base-internal](https://github.com/OmniNode-ai/knowledge-base-internal)
  instead. Open a docs PR against the relevant knowledge base repo, not
  against `omnimarket/docs/`.
- Dated point-in-time artifacts (evidence bundles, audit snapshots, execution
  tracking) that remain in this repo stay under `docs/evidence/`,
  `docs/audits/`, or `docs/tracking/` — the org's docs taxonomy classifies
  these as Bucket-D snapshots that record a specific moment rather than a
  durable fact, so they are not migrated. Scrub them the same as any other
  tracked file, and promote any durable, current fact they contain into the
  knowledge base rather than leaving it only in the snapshot.
