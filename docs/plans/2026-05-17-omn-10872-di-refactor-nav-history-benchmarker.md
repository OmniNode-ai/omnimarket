# OMN-10872 + OMN-10873: DI Refactor — NavigationHistoryWriter & LlmEvalHarness

## Tickets
- OMN-10872: HandlerNavigationHistoryReducer — inject writer via protocol
- OMN-10873: NodeOverseerBenchmarker — inject harness via protocol

## Changes

### OMN-10872
- Add `ProtocolNavigationHistoryWriter` protocol with `record()` and `close()` methods
- Update `HandlerNavigationHistoryReducer.__init__` to accept `ProtocolNavigationHistoryWriter | None`
- Concrete `HandlerNavigationHistoryWriter` satisfies the protocol structurally

### OMN-10873
- Add `ProtocolLlmEvalHarness` protocol with `handle()` method to `handler_llm_eval_harness.py`
- Update `NodeOverseerBenchmarker.__init__` to accept `ProtocolLlmEvalHarness | None`
- Concrete `NodeLlmEvalHarness` satisfies the protocol structurally

## Tests
- `tests/test_di_injection_navigation_history_reducer.py` — stub writer injected, verifies protocol path
- `tests/test_di_injection_overseer_benchmarker.py` — stub harness injected, verifies protocol path
