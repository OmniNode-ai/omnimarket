# DelegationEvalRun: TLA+ model of the delegation eval run operation (OMN-19793, plan task EV.4)

Model-before-build for the run operation (`node_delegation_eval_run_orchestrator`) and the run-completed
leg of `node_projection_delegation_eval`. Design source: the delegation evals plan, section 3 (keys)
and task EV.4 (one terminal per command; a redelivered command produces no second result).

## Properties

| Invariant | Mutation (one knob) | Config |
| -- | -- | -- |
| `SameInputsOneRun`: the eval run id is a function of manifest id, label-set sha and gate version | run id derived from the delivery | `M1_run_id_from_delivery` |
| `NoExtraRows`: a redelivered run command writes no row beyond the first delivery's | writer inserts instead of upserting | `M2_writer_inserts` |
| `OneResultPerRun`: one eval run id has one result | result depends on the delivery | `M3_result_depends_on_delivery` |
| `OneTerminalPerDelivery`, `TypeOK` | none | design only |

`DelegationEvalRun_design` checks every invariant and must pass. Each mutant must produce a
counterexample. Run `./run_all.sh`; results are in `results/`.

## Verdict (2026-09-30)

Design: no error, 1,570 distinct states, depth 13. M1 violates `SameInputsOneRun`, M2 violates
`NoExtraRows`, M3 violates `OneResultPerRun`. Design review: the handler derives the run id from
inputs only (`handler_delegation_eval_run.eval_run_id`), and the writer upserts on
(tenant, eval run id, item key) and (tenant, eval run id, class, stratum, arm), which is the design
config's writer.
