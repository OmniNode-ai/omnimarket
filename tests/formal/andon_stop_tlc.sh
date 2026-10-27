#!/usr/bin/env bash
# Runs TLC over the andon_stop model matrix and writes formal/andon_stop/results/:
# one <cfg>.out per config and SUMMARY.txt with the sha256 of the model, each config and
# each config's output.
# Usage: tests/formal/andon_stop_tlc.sh   (always the whole matrix)
# The design config must exit 0 with "No error has been found"; each mutant must report a
# counterexample to the one property its config checks. tests/formal/test_andon_stop_model.py
# reads SUMMARY.txt and fails when the model or a config changes without a re-run.
# tla2tools.jar: TLA2TOOLS_JAR when set, else the pinned release download (TLC 2.19, v1.7.4);
# the sha256 is checked either way. Java: JAVA, then PATH, then the eclipse-temurin:17-jre-alpine
# image through docker.
set -euo pipefail
cd "$(dirname "$0")/../../formal/andon_stop"
JAR_SHA=936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88
JAR_URL=https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar
MATRIX=(AndonStop_design AndonStop_design_live M1_grant_unguarded
        M2_expiry_unguarded M3_reopen_resets_budget M4_handfix_clears
        M5_clear_on_any_reading M6_unknown_counts M7_history_counts
        M8_handfix_no_evidence M9_handfix_any_occurrence M10_handfix_no_anchor
        M11_stale_clears_itself M12_stale_no_decision_row M13_operator_clears
        M14_declare_unrecorded M15_migration_clears M16_restart_forgets
        M17_dedup_by_detector M18_storm_per_key M19_refuse_open_pr_fix
        M20_refuse_swarm_lane M21_claim_by_brief M22_gate_skips_stale
        M23_observe_refuses M24_no_canary M25_claim_unguarded
        M26_receipt_unguarded M27_no_generation_check M28_revoke_on_replacement
        M29_no_stale_limit M30_no_pending_window M31_replacement_waits_on_process
        M32_release_while_swarming M33_no_stale_from_fixed M34_countermeasures_never_fix
        W1_hand_fix_reaches_cleared W2_countermeasure_reaches_cleared)
if [ ! -f tla2tools.jar ]; then
  if [ -n "${TLA2TOOLS_JAR:-}" ]; then cp "$TLA2TOOLS_JAR" tla2tools.jar
  else curl -fsSL -o tla2tools.jar "$JAR_URL"; fi
fi
echo "$JAR_SHA  tla2tools.jar" | shasum -a 256 -c - >/dev/null
if [ -n "${JAVA:-}" ]; then RUN=("$JAVA")
elif command -v java >/dev/null && java -version >/dev/null 2>&1; then RUN=(java)
elif command -v docker >/dev/null; then
  RUN=(docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" -w /work
       eclipse-temurin:17-jre-alpine java)
else echo "no working java" >&2; exit 1; fi

sha() { shasum -a 256 "$1" | cut -d' ' -f1; }

tlc() {
  local name="$1" rc=0
  "${RUN[@]}" -XX:+UseParallelGC -Xmx"${HEAP:-4g}" -cp tla2tools.jar tlc2.TLC -config "$name.cfg" \
    -workers "${WORKERS:-2}" -metadir "states/$name" -cleanup AndonStop.tla \
    > "results/$name.raw" 2>&1 || rc=$?
  sed -e "s#$PWD/##g" -e 's#/work/##g' "results/$name.raw" > "results/$name.out"
  echo "exit=$rc" >> "results/$name.out"
  rm -f "results/$name.raw"
  rm -rf "states/$name"
}

verdict() {
  local name="$1" out="results/$1.out" inv
  if grep -q '^exit=0$' "$out" && grep -q "No error has been found" "$out"; then
    echo PASS
    return
  fi
  inv="$(grep -m1 -oE '(Invariant|Action property) [A-Za-z0-9_]+ is violated' "$out" | awk '{print $(NF-2)}' || true)"
  if [ -n "$inv" ]; then
    echo "VIOLATED $inv"
  elif grep -q "Temporal properties were violated" "$out"; then
    echo "VIOLATED $(grep -m1 -E '^PROPERTY ' "$name.cfg" | cut -d' ' -f2)"
  else
    echo ERROR
  fi
}

mkdir -p results states
{
  echo "model: AndonStop.tla sha256=$(sha AndonStop.tla)"
  for c in "${MATRIX[@]}"; do
    tlc "$c"
    echo "$c cfg_sha256=$(sha "$c.cfg") out_sha256=$(sha "results/$c.out") result=$(verdict "$c")"
  done
} > results/SUMMARY.txt.new
mv results/SUMMARY.txt.new results/SUMMARY.txt
cat results/SUMMARY.txt
