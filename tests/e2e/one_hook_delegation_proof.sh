#!/bin/bash
# OMN-23413 AC41 (Amendment 14a revised, 14c): one delegation hook, proven end to end
# through the onex CLI with real claude and the free local Qwen, in an isolated
# HOME and CLAUDE_CONFIG_DIR, under a clean environment (env -i): nothing from the
# operator's shell (ONEX_STATE_DIR, OMNI_HOME, ...) reaches onex or claude.
# Writes $AC41_ROOT/artifact/run-<UTC>/ac41-result.json; exits non-zero on any failed check.
#
# Prerequisites in $AC41_ROOT (the proof never touches the operator's own setup):
#   home/    the isolated HOME, with a local model added (onex models add local ...)
#            and home/Library/Keychains linked to the login keychain (macOS),
#   claude/  the isolated CLAUDE_CONFIG_DIR, signed in once (claude auth login),
#   $AC41_VENV (default $AC41_ROOT/venv) with the onex under test on its bin/.
set -u
P="${AC41_ROOT:?set AC41_ROOT to the proof folder}"
H="$P/home"; C="$P/claude"; V="${AC41_VENV:-$P/venv}"; A="$P/artifact/run-$(date -u +%Y%m%dT%H%M%SZ)"
W="$P/work"
mkdir -p "$A" "$W"
RUN=(env -i HOME="$H" CLAUDE_CONFIG_DIR="$C" USER="$USER" LOGNAME="$USER" TMPDIR="${TMPDIR:-/tmp}"
     LANG=en_US.UTF-8 TERM=xterm-256color SHELL=/bin/zsh
     PATH="$V/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin")
DB="$H/.omninode/delegation/delegation.sqlite"
FAIL=0
check() { if [ "$2" = "1" ]; then echo "PASS $1"; else echo "FAIL $1"; FAIL=1; fi; echo "$1|$2" >> "$A/checks.txt"; }
hooks_hash() { python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(json.dumps(d.get('hooks',{}),sort_keys=True))" "$1" | shasum -a 256 | cut -c1-64; }
owned_rows() { python3 - "$C/settings.json" "$H" <<'EOF'
import json, sys
s = json.load(open(sys.argv[1])) if __import__("os").path.exists(sys.argv[1]) else {}
rows = [(ev, g.get("matcher"), h["command"]) for ev, gs in (s.get("hooks") or {}).items()
        for g in gs for h in g.get("hooks", []) if sys.argv[2] + "/" in h["command"]]
print(json.dumps(rows))
EOF
}

# 0. Real configuration before; versions.
REAL_BEFORE=$(hooks_hash "$HOME/.claude/settings.json"); REAL_SHA_BEFORE=$(shasum -a 256 "$HOME/.claude/settings.json" | cut -c1-64)
"${RUN[@]}" claude --version > "$A/claude-version.txt" 2>&1
"${RUN[@]}" claude auth status 2>/dev/null | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['loggedIn'])" > "$A/claude-logged-in.txt"
check "isolated claude signed in" "$([ "$(cat "$A/claude-logged-in.txt")" = "True" ] && echo 1 || echo 0)"

# 1. onex delegation on: exactly the prompt hook and its helper; a second on changes nothing.
"${RUN[@]}" bash -c "cd '$W' && onex delegation on --json" > "$A/01-on.json" 2> "$A/01-on.err"
check "onex delegation on reports on" "$(tail -1 "$A/01-on.json" | python3 -c "import json,sys;print(int(json.load(sys.stdin)['status']=='on'))")"
SET1=$(shasum -a 256 "$C/settings.json" | cut -c1-64)
owned_rows > "$A/02-registered.json"
check "exactly the prompt hook and its helper" "$(python3 -c "
import json;r=json.load(open('$A/02-registered.json'))
ok=len(r)==2 and all(e=='UserPromptSubmit' for e,_,_ in r) and any('ONEX_DELEGATION_HOOK=prompts' in c for _,_,c in r) and any('--history-only' in c for _,_,c in r)
print(int(ok))")"
"${RUN[@]}" bash -c "cd '$W' && onex delegation on --json" > "$A/01b-on-again.json" 2>/dev/null
check "a second on changes nothing" "$([ "$SET1" = "$(shasum -a 256 "$C/settings.json" | cut -c1-64)" ] && echo 1 || echo 0)"

# 2. A prompt answered by delegation, with its model and run id.
T0=$(date +%s)
"${RUN[@]}" bash -c "cd '$W' && timeout 420 claude -p 'In two sentences, explain what DNS does.' --output-format json" > "$A/03-delegated.json" 2> "$A/03-delegated.err"
echo $(( $(date +%s) - T0 )) > "$A/03-seconds.txt"
RUN_ID=$(python3 -c "
import json,re;d=json.load(open('$A/03-delegated.json'));m=re.search(r'Answered by (\S+) through ONEX delegation, run ([0-9a-f-]{36})',str(d.get('result')))
print(m.group(2) if m and d.get('num_turns')==0 else '')")
MODEL=$(python3 -c "
import json,re;d=json.load(open('$A/03-delegated.json'));m=re.search(r'Answered by (\S+) through',str(d.get('result')));print(m.group(1) if m else '')")
check "prompt answered by delegation (no Claude turn), model and run named" "$([ -n "$RUN_ID" ] && echo 1 || echo 0)"

# 3. A prompt that needs the machine falls to Claude.
echo "proof marker" > "$W/ac41-marker.txt"
"${RUN[@]}" bash -c "cd '$W' && timeout 420 claude -p 'Run ls in the current folder and tell me the exact file names it prints.' --allowedTools 'Bash(ls:*)' --output-format json" > "$A/04-to-claude.json" 2> "$A/04-to-claude.err"
check "machine prompt answered by Claude" "$(python3 -c "
import json;d=json.load(open('$A/04-to-claude.json'));print(int(d.get('num_turns',0)>=1 and 'ac41-marker.txt' in str(d.get('result')) and 'ONEX delegation' not in str(d.get('result'))))")"

# 4. Recorded: the receipt in the isolated state, its delegation_events row with measured tokens.
RECEIPT=$(ls "$H/.onex_state/runs/$RUN_ID/receipt.json" "$H/.local/state/omniclaude/runs/$RUN_ID/receipt.json" 2>/dev/null | head -1)
echo "$RECEIPT" > "$A/receipt-path.txt"
check "run receipt in the isolated home" "$([ -n "$RUN_ID" ] && [ -n "$RECEIPT" ] && [ -f "$RECEIPT" ] && echo 1 || echo 0)"
CID=$(python3 -c "import json;print(json.load(open('$RECEIPT')).get('correlation_id',''))" 2>/dev/null)
sqlite3 -readonly -json "$DB" "select e.correlation_id,e.model_name,e.tokens_input,e.tokens_output,e.cost_usd,e.terminal_ok,m.usage_source from delegation_events e left join llm_call_metrics m on m.correlation_id=e.correlation_id where e.correlation_id='$CID'" > "$A/05-event.json" 2>&1
check "delegation_events row with measured tokens" "$(python3 -c "
import json;r=json.load(open('$A/05-event.json'));print(int(len(r)==1 and (r[0]['tokens_output'] or 0)>0 and r[0]['usage_source']=='measured'))" 2>/dev/null || echo 0)"
check "nothing written to the operator's state" "$([ ! -e "$HOME/.onex_state/runs/$RUN_ID" ] && echo 1 || echo 0)"

# 5. The dashboard serves that run.
"${RUN[@]}" bash -c "cd '$W' && exec onex dashboard" > "$A/06-dashboard.log" 2>&1 &
DPID=$!
URL=""; TOK=""
for _ in $(seq 1 90); do
  LINE=$(grep -m1 -oE "http://127\.0\.0\.1:[0-9]+/\?token=[A-Za-z0-9_-]+" "$A/06-dashboard.log" 2>/dev/null)
  if [ -n "$LINE" ]; then URL="${LINE%%/\?token=*}"; TOK="${LINE##*token=}"; break; fi; sleep 1
done
AUTH="Authorization: Bearer $TOK"
for _ in $(seq 1 60); do [ "$(curl -s -o /dev/null -w "%{http_code}" -m 3 -H "$AUTH" "$URL/projections")" = "200" ] && break; sleep 1; done
SAV="onex.snapshot.projection.delegation.savings.v1"
curl -s -m 20 -H "$AUTH" "$URL/projection/$SAV?limit=10" > "$A/06-savings.json"
python3 - "$A/06-savings.json" "$CID" > "$A/06-dashboard-run.json" <<'EOF'
import json, sys
rows = json.load(open(sys.argv[1])).get("rows") or []
hit = [s for r in rows for s in (r.get("sessions") or []) if s.get("session_id") == sys.argv[2]]
print(json.dumps(hit[0] if hit else {}))
EOF
check "dashboard serves the run with measured tokens" "$(python3 -c "
import json;s=json.load(open('$A/06-dashboard-run.json'));print(int(bool(s) and s.get('usage_source')=='measured' and (s.get('completion_tokens') or 0)>0))")"
kill "$DPID" 2>/dev/null; wait "$DPID" 2>/dev/null

# 6. Error chain: the model unreachable, Claude answers within the hook's limit.
O="$H/.omninode/delegation/bifrost_overrides.yaml"; cp "$O" "$A/bifrost_overrides.saved.yaml"
sed -i '' -E 's#endpoint_url: http://[^[:space:]]+#endpoint_url: http://127.0.0.1:9#' "$O"
T0=$(date +%s)
"${RUN[@]}" bash -c "cd '$W' && timeout 420 claude -p 'In one sentence, what is an IP address?' --output-format json" > "$A/07-unreachable.json" 2> "$A/07-unreachable.err"
SECS=$(( $(date +%s) - T0 )); echo "$SECS" > "$A/07-seconds.txt"
cp "$A/bifrost_overrides.saved.yaml" "$O"
check "model unreachable: Claude answers within the hook timeout" "$(python3 -c "
import json;d=json.load(open('$A/07-unreachable.json'));print(int(d.get('num_turns',0)>=1 and not d.get('is_error') and 'ONEX delegation' not in str(d.get('result')) and $SECS<=330))")"

# 7. onex delegation off removes the whole hook, history helper included (AC43).
"${RUN[@]}" bash -c "cd '$W' && onex delegation off --json" > "$A/08-off.json" 2> "$A/08-off.err"
owned_rows > "$A/08-registered-after-off.json"
check "off removes the whole hook, history helper included" "$([ "$(cat "$A/08-registered-after-off.json")" = "[]" ] && echo 1 || echo 0)"
python3 -c "
import json;r=json.load(open('$A/08-registered-after-off.json'));print(json.dumps({'history_helper_kept_after_off': any('--history-only' in c for _,_,c in r)}))" > "$A/08-helper-after-off.json"

# 8. The operator's real Claude configuration is unchanged.
check "operator's real Claude hooks unchanged" "$([ "$REAL_BEFORE" = "$(hooks_hash "$HOME/.claude/settings.json")" ] && echo 1 || echo 0)"
check "operator's real settings.json byte-identical" "$([ "$REAL_SHA_BEFORE" = "$(shasum -a 256 "$HOME/.claude/settings.json" | cut -c1-64)" ] && echo 1 || echo 0)"

python3 - "$A" "$RUN_ID" "$CID" "$MODEL" "$URL" <<'EOF'
import json, pathlib, sys
a = pathlib.Path(sys.argv[1])
checks = [line.rsplit("|", 1) for line in (a / "checks.txt").read_text().splitlines()]
out = {
  "ticket": "OMN-23413", "criterion": "AC41 (Amendment 14a revised)",
  "claude_code": (a / "claude-version.txt").read_text().strip(),
  "registered_on": json.loads((a / "02-registered.json").read_text()),
  "delegated_run_id": sys.argv[2], "correlation_id": sys.argv[3], "model": sys.argv[4],
  "delegated_seconds": (a / "03-seconds.txt").read_text().strip(),
  "event": json.loads((a / "05-event.json").read_text() or "[]"),
  "dashboard_url": sys.argv[5],
  "dashboard_run": json.loads((a / "06-dashboard-run.json").read_text() or "{}"),
  "unreachable_seconds": (a / "07-seconds.txt").read_text().strip(),
  "after_off": json.loads((a / "08-helper-after-off.json").read_text()),
  "checks": {name: ok == "1" for name, ok in checks},
}
out["passed"] = all(out["checks"].values())
(a / "ac41-result.json").write_text(json.dumps(out, indent=2) + "\n")
print(json.dumps({"artifact": str(a / "ac41-result.json"), "passed": out["passed"]}))
EOF
exit $FAIL
