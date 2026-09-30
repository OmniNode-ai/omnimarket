#!/bin/bash
# OMN-20032: blind raters under rubric ev4-blind-v1 over one wave's batches; opus and sonnet run side by side.
W=$(cd "$(dirname "$0")" && pwd); cd "$W"
export PATH=$HOME/.local/bin:$PATH; unset ANTHROPIC_API_KEY
mkdir -p grades
( ls batches/*.txt | xargs -P 4 -I{} ./grade.sh claude-opus-5-5 opus {}; echo OPUS_DONE ) > grade_opus.log 2>&1 &
( ls batches/*.txt | xargs -P 4 -I{} ./grade.sh claude-sonnet-5-5 sonnet {}; echo SONNET_DONE ) > grade_sonnet.log 2>&1 &
wait
touch DONE
