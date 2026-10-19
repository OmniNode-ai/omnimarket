# AgentCallAdapter: TLA+ model of the Agent-call adapter and the client-call claim (OMN-21096)

Model-before-build for Track B of the context and delegation adapter plan (task TB.1): the
Claude Code binding and adapter that intercept a model-raised foreground Agent call, and the
delegate-skill orchestrator's client-call claim row that makes the call dispatch at most once.
The later build tasks (the admission compute, the orchestrator's client section and claim
table, the adapter itself) cite this model.

## What is modelled

One intercepted Agent call ("orig") and, optionally, a second command ("conf") with the same
client_call_key and a different request fingerprint.

Client side: the binding runs the adapter, or the adapter never starts (the binding's failure
table, rendered from the host's max mode, denies on a required host). The adapter resolves its
max mode; at observe it journals the command and returns proceed_native at once; at selective
or required it publishes the command (or is refused before publish) and maps the admission
event and the terminal through the verdict table to proceed_native, answer or refuse.
Adapter states: idle, received, published, awaiting (admitted, waiting for the terminal),
answered, refused, native. A binding reload re-runs the adapter for the same tool call and
republishes the same command.

Server side: one claim row per client_call_key with states claimed, admitted, not_admitted,
observed, dispatch_intended, terminal. Every transition is a guarded write on the row's
generation. The claim is INSERT ... ON CONFLICT DO NOTHING; a second command with the same key
republishes a stored terminal, waits on a live lease, takes over an expired lease, or (a
different fingerprint) gets a client_call_conflict terminal under its own correlation id with
no write. The admission (effective mode = min(client max, server mode); admitted when the task
type is allowlisted, not sensitive and the mode is not observe) commits, then its event is
published; the dispatch intent commits, then the delegation request is published; the chain's
terminal, or reconciliation past the deadline (dispatch_outcome_unknown), is stored, then
published. The reconciler takes over any non-terminal row whose lease expired and resumes
from the recorded state; a dispatch_intended row is never dispatched again.

Faults: a crash between every pair of orchestrator steps (an un-acked delivery is redelivered),
lease expiry with the old owner still running and writing (stale owner after takeover), broker
and drainer redelivery, a binding reload re-run, a conflicting command, one server mode change
at any point, and every combination of client max mode, initial server mode, allowlist and
sensitivity (chosen in Init, so client and server disagree on mode in some behaviours).
Bounds: MaxCrash 1, MaxExpire 1, MaxRedeliver 1, MaxRerun 1, MaxModeChange 1, MaxGen 3.

## Properties

The six invariants of the plan's Track B design, plus TypeOK.

| Invariant | Mutation (one knob set FALSE) | Config |
| -- | -- | -- |
| `AtMostOneDispatchPerClientCall` | no generation fencing on writes | `M1_no_fencing` |
| `AtMostOneDispatchPerClientCall` | delegation request published before the intent commits | `M2_dispatch_before_intent` |
| `AtMostOneDispatchPerClientCall` | the claim resets a live row (blind upsert) | `M3_blind_upsert` |
| `AtMostOneDispatchPerClientCall` | a takeover of dispatch_intended dispatches again | `M4_reconcile_redispatch` |
| `ConflictNeverReachesOriginal` | correlation id from the key alone | `M5_corr_from_key` |
| `NativeOnlyWhenConfirmedNotDispatched` | admission event published before its commit | `M6_echo_before_commit` |
| `ObserveNeverDispatches` | effective mode is the server mode alone | `M7_server_mode_wins` |
| `NativeOnlyWhenConfirmedNotDispatched` | selective proceeds native on admission unknown | `M8_native_on_unknown` |
| `RequiredAllowlistedNeverNative` | adapter never started maps to next(e) on a required host | `M9_binding_fail_open` |
| `AnswerOnlyOnAcceptedResult` | answer on any terminal | `M10_answer_any_terminal` |

Reading of two invariants: `NativeOnlyWhenConfirmedNotDispatched` is checked as "the adapter
is native and the call was dispatched" never holding together, which covers a dispatch after
the native decision. `RequiredAllowlistedNeverNative` allows native on a required host only
when the server echoed a lowered effective mode for the call (a server rollback to observe
applies to every command not yet admitted).

`AgentCallAdapter_design` checks every invariant and must pass; each mutant checks only its
target and must produce a counterexample. Run `tests/formal/agent_call_adapter_tlc.sh`;
results are in `results/`, and `tests/formal/test_agent_call_adapter_model.py` checks them
against the committed model and configs.

## Result (2026-10-17)

Design: no error, 63,029,693 distinct states, depth 30. Each of the ten mutants violates the
invariant its row names (results/SUMMARY.txt).

Not modelled: liveness (every claim row reaching terminal under fair reconciliation), more
than one intercepted call, and timing values; the deadlines are nondeterministic timeouts.
