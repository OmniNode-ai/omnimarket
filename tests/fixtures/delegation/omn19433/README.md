# OMN-19433 recorded delegation fixture

Excerpted from local delegation run `01bd1d20-7ca7-41eb-96eb-f24964c15f38`
(in-process, task class `document`, 2026-09-22). The run rendered an hourly
comment-reply sweep report from a JSON facts block. Three local attempts were
refused by the blocking `accurate` rule with
`TASK_MISMATCH: response explicitly disclaims accuracy: unverified@offset=6067`,
and the run failed.

The word came from the input. One facts row carried a reason that said "none
is marked UNVERIFIED", and the report quoted that reason, as it was told to.
The answer disclaimed nothing.

| File | What it is |
| -- | -- |
| `01bd1d20_grounding_source_excerpt.txt` | The instruction header of the recorded prompt, verbatim, and the facts row that carries the word, cut to its first three sentences. |
| `01bd1d20_response_excerpt.txt` | The report line that quotes that row, cut to the same three sentences, under a header. |

## Why these are excerpts

The recorded prompt (16,841 bytes, sha256 `ee11a8ae5800124f7e7ce60f1a098ab28632352966543b5d41ad2957a880bdd3`)
and the answer returned to the caller (13,430 bytes, sha256
`82e8c71ffb02b6873278ab1619b979453d63921f55ca084d5f1d410b7cb82a49`) carry
internal ticket links and secret-store paths that do not belong in this
repository. The veto depends only on the text around the matched phrase, so
the excerpts keep that text byte for byte: in both files the phrase sits
inside the same sentence, with the same words on either side. The full pair
was replayed through the gate before this fixture was committed, and the
pull request records that replay.
