# Capability catalog

generated_at: 2026-09-27T15:40:00Z

## Hosts

| Host | State |
| - | - |
| lab-101 (lab-101) | up |
| lab-105 (lab-105) | up |
| lab-200 (lab-200 (orchestrator host)) | up |
| lab-201 (lab-201 (.201)) | up |
| lab-202 (lab-202 (.202)) | OUT OF SERVICE (RULING 2026-09-27T13:01:20Z: held out of the tier ladder) |

## Lanes

| Host | Lane | Status | Note |
| - | - | - | - |
| lab-200 | dev-200 | FREE | 28 containers, 23 up |
| lab-200 | dogfood | BUSY | 10 containers |
| lab-201 | dev | UNKNOWN | ssh exit 124 |
| lab-201 | prepr-1 | FREE | ephemeral slot, idle |

## Engines

| Engine | State | Invocation |
| - | - | - |
| Codex | ok | `codex exec --sandbox workspace-write -c model_reasoning_effort="high" "<prompt>"` |
| onex delegate | ok | `"$OMNI_HOME/omnibase_infra/scripts/onex" delegate "<prompt>" --task-type <class> --ticket <ticket> --bus kafka --lane dev --locus deployed-lane --state-root "$OMNI_HOME/.onex_state"` |
| GLM | down | `onex delegate --task-type <class> --model-pin glm "<prompt>"` |
| Jev (TypeSafe) | unknown | `https api.typesafe.ai/v1/systemone (typed decisions only)` |
| crush | unknown | `crush "<prompt>"` |
| OpenRouter | unknown | `onex delegate --task-type <class> --provider openrouter "<prompt>"` |
| Gemini | unknown | `onex delegate --task-type <class> --provider gemini "<prompt>"` |

## Front-door skills

- `omni:way-of-working`
- `omni:session-start`
- `omni:capabilities`
- `omni:ledger-write`
- `omni:ledger-msg`
- `omni:pr-land`
- `omni:merge-drain`
- `omni:create-ticket`
- `omni:lane-brief`

## Launchd jobs

| Label | State | Successor |
| - | - | - |
| ai.omninode.hourly-tick | loaded-ok |  |
| ai.omninode.planner-example | loaded-failing |  |
| ai.omninode.pr-status-sweep | dead | ai.omninode.pr-watcher |

## Plugins

| Plugin | Installed | Available |
| - | - | - |
| omni | 0.4.51 | 0.4.53 |

## Credential refs

| Ref | Provider |
| - | - |
| llm.gemini.api_key | gemini |
| llm.glm.api_key | glm |
| llm.openrouter.api_key | openrouter |
| llm.typesafe.api_key | typesafe |
