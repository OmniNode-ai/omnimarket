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

## Sibling Repository Pins In CI

A workflow in this repository reads a sibling repository only at the version
this repository has pinned, never at the sibling's live branch, so a merge in
one repository cannot turn another repository's checks red.

| Sibling | Pin | Where it lives |
| --- | --- | --- |
| `omnibase_core`, `omnibase_compat`, `omnibase_spi`, `omnimemory` | release tag `v<version>` of the `uv.lock` entry | each clone step reads the version from `uv.lock` |
| `omnibase_infra` | release tag matching the `omnibase-infra` entry in `uv.lock` | literal `ref:` in each checkout step |
| `omniclaude` | release tag `v0.27.0` | literal `ref:` in each checkout step; the 40-char sha of the tag plus a `# <tag>` comment in each `uses:` reference |
| `omniintelligence` | commit sha of its `dev` tip when last bumped (no release carries the hostile-review model roster this repo requests) | literal `ref:` in the contract-topic-graph checkout and `clone_with_retry` in the hostile-review workflow |
| `onex_change_control` | commit sha of its `dev` tip when last bumped (its governance allowlists and lint rules ship ahead of its tags) | literal `ref:` in each checkout and clone step; the sha in each `uses:` reference |
| `omnibase_core` reusable workflow | commit sha of the `omnibase-core` lock version's tag | `uses:` in `docs-validate.yml` |

To move a pin, bump the lock or the literal tag in one PR, run the affected
workflows against the new version, and land it only when they pass. A sibling's
breaking change reaches this repository as a consumer PR that moves the pin; it
is never picked up by a branch moving. The sibling-pin test under `tests/ci/`
fails when a workflow step names a sibling without a pinned version, and when a
literal pin drifts from `uv.lock`.
