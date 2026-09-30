---------------------------- MODULE DelegationEvalRun ----------------------------
(* OMN-19793 (EV.4) model before build.
   The run operation (node_delegation_eval_run_orchestrator) and the rule-7a writer
   of node_projection_delegation_eval, under at-least-once command delivery.

   Facts modelled:
   * A run command names a manifest, a label set and a gate version. The eval run
     id is a pure function of those three (uuid5 in the code), never of the
     delivery.
   * Each delivery of a command ends in exactly one terminal: one run-completed
     event (status completed or failed). A delivery never ends silent.
   * The broker may redeliver any command any number of times (bounded here),
     and may redeliver any published event to the writer.
   * The writer upserts rows keyed by (eval run id, row key). A row set is a
     pure function of the result.

   Mutations (each must violate an invariant, see M*.cfg):
   * RunIdFromDelivery: the run id also depends on the delivery count.
   * WriterInserts: the writer appends instead of upserting.
   * ResultDependsOnDelivery: the handler's result depends on when it ran
     (for example, labels read at delivery time under the same run id). *)
EXTENDS Naturals, FiniteSets, Sequences

CONSTANTS MaxDeliveries,
          RunIdFromDelivery, WriterInserts, ResultDependsOnDelivery

VARIABLES delivered,   \* command id -> number of deliveries handled
          events,      \* sequence of published events [cmd, run, result, delivery]
          consumed,    \* number of event deliveries the writer has applied (may re-apply)
          rows         \* set (or bag, when WriterInserts) of rows [run, key, val]

vars == <<delivered, events, consumed, rows>>

\* c1 and c2 carry the same inputs (a redelivered or duplicated command); c3 names a new gate version.
Commands == {"c1", "c2", "c3"}
Inputs == [c \in Commands |-> IF c = "c3" THEN "gate_v2" ELSE "gate_v1"]

RunId(c, d) == IF RunIdFromDelivery THEN <<Inputs[c], d>> ELSE <<Inputs[c], 0>>
Result(c, d) == IF ResultDependsOnDelivery THEN <<Inputs[c], d>> ELSE <<Inputs[c], 0>>
\* Two row keys per result: one results row and one item-verdict row.
RowsOf(run, res) == { [run |-> run, key |-> k, val |-> res] : k \in {"result", "verdict"} }

Init == /\ delivered = [c \in Commands |-> 0]
        /\ events = <<>>
        /\ consumed = 0
        /\ rows = {}

\* The handler takes one delivery of command c and publishes exactly one terminal.
Deliver(c) ==
  /\ delivered[c] < MaxDeliveries
  /\ LET d == delivered[c] + 1 IN
       /\ delivered' = [delivered EXCEPT ![c] = d]
       /\ events' = Append(events, [cmd |-> c, run |-> RunId(c, d), result |-> Result(c, d), delivery |-> d])
  /\ UNCHANGED <<consumed, rows>>

Upsert(rs, new) ==
  IF WriterInserts
  THEN rs \cup { [r EXCEPT !.key = <<r.key, Cardinality(rs)>>] : r \in new }
  ELSE { r \in rs : ~\E n \in new : n.run = r.run /\ n.key = r.key } \cup new

\* The writer applies the next event, or re-applies an earlier one (redelivery).
Apply(i) ==
  /\ i \in 1..Len(events)
  /\ i <= consumed + 1
  /\ consumed' = IF i = consumed + 1 THEN consumed + 1 ELSE consumed
  /\ rows' = Upsert(rows, RowsOf(events[i].run, events[i].result))
  /\ UNCHANGED <<delivered, events>>

Next == \/ \E c \in Commands : Deliver(c)
        \/ \E i \in 1..MaxDeliveries * Cardinality(Commands) : Apply(i)

Spec == Init /\ [][Next]_vars

TypeOK == /\ delivered \in [Commands -> 0..MaxDeliveries]
          /\ consumed \in 0..Len(events)

\* One terminal per delivery: the event count equals the deliveries taken.
OneTerminalPerDelivery ==
  Len(events) = LET S == Commands IN
    LET F[T \in SUBSET S] == IF T = {} THEN 0 ELSE LET x == CHOOSE y \in T : TRUE IN delivered[x] + F[T \ {x}]
    IN F[S]

\* Commands with the same inputs map to one run id.
SameInputsOneRun ==
  \A i, j \in 1..Len(events) :
    Inputs[events[i].cmd] = Inputs[events[j].cmd] => events[i].run = events[j].run

\* A run id carries exactly one result: a redelivered command produces no second, different result.
OneResultPerRun ==
  \A i, j \in 1..Len(events) : events[i].run = events[j].run => events[i].result = events[j].result

\* A redelivered command or event adds no row: each run holds exactly its two keys.
NoExtraRows ==
  \A run \in { r.run : r \in rows } : Cardinality({ r \in rows : r.run = run }) = 2
=============================================================================
