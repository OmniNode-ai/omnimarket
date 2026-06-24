# DI Refactor — NavigationHistoryWriter and LlmEvalHarness

## Overview

Add protocol-based injection seams to two handlers that previously only
accepted concrete types, enabling test doubles and alternative implementations
to be injected without subclassing.

## Changes

### NavigationHistoryReducer
- Add `ProtocolNavigationHistoryWriter` protocol with `record()` and `close()` methods
- Update `HandlerNavigationHistoryReducer.__init__` to accept `ProtocolNavigationHistoryWriter | None`
- Concrete `HandlerNavigationHistoryWriter` satisfies the protocol structurally

### OverseerBenchmarker
- Add `ProtocolLlmEvalHarness` protocol with `handle()` method to `handler_llm_eval_harness.py`
- Update `NodeOverseerBenchmarker.__init__` to accept `ProtocolLlmEvalHarness | None`
- Concrete `NodeLlmEvalHarness` satisfies the protocol structurally

## Tests
- `tests/test_di_injection_navigation_history_reducer.py` — verifies protocol injection path
- `tests/test_di_injection_overseer_benchmarker.py` — verifies protocol injection path
