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
  runbook, and operator-facing content is not part of this repository. Open a
  docs PR against the knowledge base, not against `omnimarket/docs/`.
- Dated point-in-time artifacts do not belong in this repository at all, and
  the `docs/evidence/`, `docs/audits/` and `docs/tracking/` directories are
  being retired rather than sanctioned. This bullet used to say the opposite,
  and that sentence is why those directories grew: a contributor following this
  guide produced exactly the material now being removed from them.
  - Evidence and DoD receipts go to the change-control repository, which is
    already where the receipt gate resolves them from.
  - Tracking, status, plans, deep dives, handoffs and reports go to the
    internal documentation home for their class.
  - Nothing dated is added under `docs/`. If you find yourself wanting to,
    the artifact has a home and this repository is not it.
