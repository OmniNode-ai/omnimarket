# OmniMarket Documentation

Start with the root [README](../README.md) for install, common commands, and
the repo-level ownership boundary.

## Current Architecture

- [Package model](architecture/package-model.md) - ONEX package layers,
  runtime modes, and contract package shape.
- [Skill, package, and node boundaries](architecture/skill-vs-package-vs-node.md)
  - what belongs in wrapper repos versus OmniMarket.
- [Dependency boundary](architecture/dependency-boundary.md) - root dependency
  scope, node metadata dependencies, and current isolation limits.

## Reference

- [Node catalog](reference/node-catalog.md) - node families, entry-point
  source of truth, and discovery commands.
- [Node metadata reference](reference/node-metadata.md) - required metadata
  fields and package capability conventions.
- [Node testing](node-testing.md) - skill-to-node dispatch parity and
  golden-chain test expectations.

## Migrations

- [Build-loop migration boundary](migrations/build-loop.md) - what moved into
  Market, what remains runtime-owned, and what still requires external wiring.

## Runbooks

Current run commands live in the root README because they are developer
workflows rather than operator runbooks. Add a runbook here only when it
describes a current operational procedure with commands and expected evidence.

## Decisions

Stable current decisions are promoted into the architecture docs above. Dated
design and tracking files are not public entrypoints.

## Historical Context

Historical execution tracking, evidence notes, and one-off post-mortems are not
kept in this public docs tree. Current repo facts from those files have been
promoted into stable docs where they still apply.
