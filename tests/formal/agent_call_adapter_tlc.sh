#!/usr/bin/env bash
# Runs TLC over the agent_call_adapter model matrix and writes formal/agent_call_adapter/results/:
# one <cfg>.out per config and SUMMARY.txt with the sha256 of the model, the tla2tools.jar,
# each config and each config's output.
# Usage: tests/formal/agent_call_adapter_tlc.sh   (always the whole matrix)
# The design config must exit 0 with "No error has been found"; each mutant must report a
# counterexample to the one property its config checks. tests/formal/test_agent_call_adapter_model.py
# reads SUMMARY.txt and fails when the model or a config changes without a re-run.
# tla2tools.jar: TLA2TOOLS_JAR when set, else the pinned release download (TLC 2.19, v1.7.4);
# its sha256 must be one of JAR_SHAS. Java: JAVA, then PATH, then the eclipse-temurin:17-jre-alpine
# image through docker.
set -euo pipefail
cd "$(dirname "$0")/../../formal/agent_call_adapter"
# v1.7.4 release, and the 2026.10.13 nightly (rev 210df3a) the committed results were made with
JAR_SHAS=(936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88
          0f56d6a813368c48008dfbecb23a73ab43a4a5e1bf09f026782a674aa9d4b3c9)
JAR_URL=https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar
MATRIX=(AgentCallAdapter_design M1_no_fencing M2_dispatch_before_intent M3_blind_upsert
        M4_reconcile_redispatch M5_corr_from_key M6_echo_before_commit M7_server_mode_wins
        M8_native_on_unknown M9_binding_fail_open M10_answer_any_terminal)
if [ ! -f tla2tools.jar ]; then
  if [ -n "${TLA2TOOLS_JAR:-}" ]; then cp "$TLA2TOOLS_JAR" tla2tools.jar
  else curl -fsSL -o tla2tools.jar "$JAR_URL"; fi
fi

sha() { shasum -a 256 "$1" | cut -d' ' -f1; }

jar_sha="$(sha tla2tools.jar)"
case " ${JAR_SHAS[*]} " in *" $jar_sha "*) ;; *) echo "tla2tools.jar sha256 $jar_sha not pinned" >&2; exit 1 ;; esac
if [ -n "${JAVA:-}" ]; then RUN=("$JAVA")
elif command -v java >/dev/null && java -version >/dev/null 2>&1; then RUN=(java)
elif command -v docker >/dev/null; then
  RUN=(docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" -w /work
       eclipse-temurin:17-jre-alpine java)
else echo "no working java" >&2; exit 1; fi

tlc() {
  local name="$1" rc=0
  "${RUN[@]}" -XX:+UseParallelGC -Xmx"${HEAP:-4g}" -cp tla2tools.jar tlc2.TLC -config "$name.cfg" \
    -workers "${WORKERS:-2}" -metadir "states/$name" -cleanup -noGenerateSpecTE AgentCallAdapter.tla \
    > "results/$name.raw" 2>&1 || rc=$?
  sed -e "s#$PWD/##g" -e 's#/work/##g' "results/$name.raw" > "results/$name.out"
  echo "exit=$rc" >> "results/$name.out"
  rm -f "results/$name.raw"
  rm -rf "states/$name"
}

verdict() {
  local out="results/$1.out" inv
  if grep -q '^exit=0$' "$out" && grep -q "No error has been found" "$out"; then
    echo PASS
    return
  fi
  inv="$(grep -m1 -oE 'Invariant [A-Za-z0-9_]+ is violated' "$out" | cut -d' ' -f2 || true)"
  if [ -n "$inv" ]; then echo "VIOLATED $inv"; else echo ERROR; fi
}

mkdir -p results states
{
  echo "model: AgentCallAdapter.tla sha256=$(sha AgentCallAdapter.tla)"
  echo "tla2tools: sha256=$jar_sha"
  for c in "${MATRIX[@]}"; do
    tlc "$c"
    echo "$c cfg_sha256=$(sha "$c.cfg") out_sha256=$(sha "results/$c.out") result=$(verdict "$c")"
  done
} > results/SUMMARY.txt.new
mv results/SUMMARY.txt.new results/SUMMARY.txt
cat results/SUMMARY.txt
