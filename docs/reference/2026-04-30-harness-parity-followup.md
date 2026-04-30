# Harness Parity Follow-Up

## Boundary

The market skill is the authoritative execution boundary for CLI report generation. Harnesses invoke the market skill, and the market returns a typed `ModelMarketCliReport`.

Harnesses may render the `ModelMarketCliReport` for local output, persist it as evidence, or forward it to another consumer. Harnesses do not own the report contract and should not create a parallel report shape.

## Out of Scope

No harness implementation work is included in this plan. This follow-up only records the parity boundary and the candidate tickets needed to bring external harnesses into alignment.

This plan does not change Codex, Claude, or plugin runtime behavior. It does not add shims, adapters, renderers, forwarding code, or compatibility migrations.

## Follow-Up Tickets

- Codex harness parity: invoke the market skill and consume `ModelMarketCliReport` without introducing a Codex-specific report contract.
- Claude plugin parity: invoke the market skill and consume `ModelMarketCliReport` without introducing a Claude-specific report contract.
- Shared renderer parity: define any presentation-only rendering behavior as a consumer of `ModelMarketCliReport`, not as a source of truth.
