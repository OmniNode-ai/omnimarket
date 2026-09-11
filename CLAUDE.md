# CLAUDE.md - OmniMarket

This file gives agent-specific working guidance for this repository. The public
architecture and onboarding entrypoint is `README.md`.

## Repo Role

OmniMarket owns portable ONEX workflow packages and contract-driven automation
logic. Platform wrappers may invoke Market nodes, but wrapper instructions must
not become the long-lived owner for workflow business logic.

## Development Rules

- Use Python 3.12 and `uv`.
- Prefer repo-local patterns before introducing a new abstraction.
- Keep node logic inside `src/omnimarket/nodes/node_*`.
- Keep shared cross-node models in shared packages such as
  `omnimarket.events`, `omnimarket.intelligence`, `omnimarket.projection`,
  `omnimarket.routing`, or `omnimarket.models`.
- Do not make one node import another node's private handler or model package.
  Promote shared types instead.
- Keep event topics declared in `contract.yaml`; avoid hardcoded topic strings
  in handlers.
- Update `metadata.yaml` when a node gains dependencies, capabilities, package
  grouping, display name, or entry flags.
- Do not add public docs that point to private workspaces, ticket URLs, ticket
  identifiers, old transfer notes, or historical execution notes.

## Common Commands

```bash
uv sync --all-extras
uv run pytest tests/ -v --tb=short -m "not kafka"
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/omnimarket/ --strict
uv run python -m omnimarket.nodes.node_runtime_sweep --import-check
uv run python scripts/ci/check_node_metadata_dependencies.py
```

## Pre-push runs no tests (retired 2026-09-10)

The pre-push test leg was retired in this repo on 2026-09-10 under OMN-18162,
phase 1 of the CI runner placement and pre-push retirement plan. Pre-push is now
a strict mypy type check and nothing else, and finishes in seconds. Hosted CI is
the enforced merge gate and the test surface. Run tests locally when you want
them, with the commands above; nothing runs them for you at `git push`.

The impacted-test selector itself is retained at
`scripts/hooks/prepush_smart_tests.sh` for manual invocation. Rollback is a
`git revert` of the retirement squash. There is no environment variable that
turns the leg back on, deliberately, and no dual path.

If a push is refused with a complaint that this repo's pre-commit configuration
is missing the governed pre-push hook, the refusal comes from a superseded
pre-push bootstrap left installed at `$GIT_COMMON_DIR/hooks/pre-push`. Delete
that file, or re-run `scripts/hooks/install_prepush_hook.py --install`. Do not
work around it.

**This is a dated pointer, not the doctrine rewrite.** The full rewrite lands
after the plan's phase-0 report, because phase 1 is reversible and coupling the
rules to a revertible change means a revert silently reverts the rules. Until
then, treat any standing instruction naming the governed impacted-test selector
as the final local pre-push check as superseded for this repo.

## Adding A Node

1. Create `src/omnimarket/nodes/node_<name>/`.
2. Add `__init__.py`, `contract.yaml`, `metadata.yaml`, and handler modules.
3. Register the package in `[project.entry-points."onex.nodes"]`.
4. Add a golden-chain or focused contract test under `tests/`.
5. Run the runtime sweep and metadata dependency check.

## Boundary Checks

Before opening a documentation or node PR, check:

```bash
find docs -type d -empty -print
```

The command should not report empty documentation directories. Also review
changed public docs for private workspace links, ticket-system URLs, ticket
identifiers, and historical execution notes.
