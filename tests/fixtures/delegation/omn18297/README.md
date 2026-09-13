# OMN-18297 recorded delegation fixture

Recorded from local delegation run `d715f096-27b9-444f-9355-3554819ef8a5`
(tier `local`, backend `local-heavy-reasoning`, model `Qwen3.6-35B-A3B`,
2026-09-13). Tokens in/out 18245/11401. The quality gate scored this exact
response `passed=true score=1.0`.

| File | What it is |
| -- | -- |
| `d715f096_grounding_source.txt` | The 22 coordination-ledger rows fed to the delegation. The delegated prompt was these rows plus a 521-byte instruction preamble, 57,120 bytes in total at the time of the run. |
| `d715f096_response_answer_segment.txt` | The response's ANSWER SEGMENT, verbatim. |

## Why the response is the answer segment and not the raw response

The raw response was 25,151 characters, of which 21,228 (84%) were the model's
own reasoning trace ahead of a closing think tag with no matching opening tag.
That leak is OMN-18278, which is open and is deliberately NOT fixed by
OMN-18297. The full raw response including the trace was not captured verbatim
at the time; the answer segment was. The grounding check reads only the segment
after the last stray terminator anyway (see `answer_segment` in the gate node's
contract), so the fixture is the text the check evaluates.

## What the recorded pair proves

Eight pull-request citations in the answer occur nowhere in the source in any
form, and every ticket identifier in the answer is grounded.

The original finding put the fabricated count at roughly twenty. That count was
an artifact of grepping the repository-qualified spelling: the source cites
pull requests in two forms, `omnibase_infra#3465` and a bare `#1407` inside an
`omninode_infra` row, so thirteen citations the model resolved correctly from a
bare number read as absent. Grounding is looked up by number token for exactly
this reason, and the honest count is eight.

## Two redactions, and why they do not weaken the claim

Two literals were replaced before this file was committed, because the
leaked-literals gate forbids them in this package and a per-file exemption would
have been a suppression rather than a fix:

| Original class | Replacement |
| -- | -- |
| an operator GitHub handle | `REDACTED-OPERATOR` |
| an EC2 instance id | `REDACTED-INSTANCE-ID` |

Neither string is a member of any declared identifier class, neither is cited by
the recorded response, and the grounding verdict is byte-identical with and
without them: the same eight pull-request citations, out of 129 identifier
occurrences checked. Nothing else in either file was altered.
