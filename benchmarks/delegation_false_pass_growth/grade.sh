#!/bin/bash
# usage: grade.sh <model> <tag> <batchfile>
S=$(dirname "$0"); b=$(basename "$3" .txt); out="$S/grades/$2_$b.json"
[ -s "$out" ] && python3 -c "import json,sys;json.loads(json.load(open('$out'))['result'].strip().removeprefix('\`\`\`json').removesuffix('\`\`\`'))" 2>/dev/null && exit 0
for try in 1 2 3; do
  claude -p --model "$1" --system-prompt-file "$S/rubric.md" --tools "" --no-session-persistence --strict-mcp-config --output-format json < "$3" > "$out.tmp" 2> "$out.err" && mv "$out.tmp" "$out" && exit 0
  sleep 20
done
