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
- [Delegation dispatch](architecture/delegation-dispatch.md) - migrated to the
  OmniNode knowledge base; this file is a pointer.
- [Delegation routing contract](architecture/delegation-routing-contract.md) -
  migrated to the OmniNode knowledge base; this file is a pointer.
- [Event registry](architecture/event-registry.md) - canonical event registry,
  drift gate, and contributor compliance path.

## Reference

- [Node catalog](reference/node-catalog.md) - migrated to the OmniNode
  knowledge base; this file is a pointer.
- [Node metadata reference](reference/node-metadata.md) - required metadata
  fields and package capability conventions.
- [Node testing](node-testing.md) - skill-to-node dispatch parity and
  golden-chain test expectations.

## Patterns

- [Skill-backing node handler](patterns/skill_backing_node_pattern.md) - canonical
  shape for skill-backing node handlers: dispatch record persistence, required
  input/output model fields, and why handlers must not call Agent() directly.

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

Dated point-in-time artifacts — execution-tracking logs, evidence bundles,
audit snapshots, and the ADR-canary ground-truth corpus — are kept in-repo
under `docs/evidence/`, `docs/audits/`, `docs/tracking/`, and
`docs/adr-canary/`. Under the org's docs taxonomy these are Bucket-D
snapshots: they stay put rather than migrating to the knowledge base, because
their value is recording a specific moment, and updating them would destroy
that. They are still subject to the same hygiene scrubbing as any other
tracked file. Current, durable repo facts drawn from those snapshots are
promoted into the stable docs above rather than left only in the snapshot.
