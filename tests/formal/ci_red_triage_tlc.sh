#!/usr/bin/env bash
# Runs TLC over the ci_red_triage model matrix and writes formal/ci_red_triage/results/:
# one <cfg>.out per config and SUMMARY.txt with the sha256 of the model, each config and
# each config's output.
# Usage: tests/formal/ci_red_triage_tlc.sh   (always the whole matrix)
# The design config must exit 0 with "No error has been found"; each mutant must report a
# counterexample to the one property its config checks. tests/formal/test_ci_red_triage_model.py
# reads SUMMARY.txt and fails when the model or a config changes without a re-run.
# tla2tools.jar: TLA2TOOLS_JAR when set, else the pinned release download (TLC 2.19, v1.7.4);
# the sha256 is checked either way. Java: JAVA, then PATH, then the eclipse-temurin:17-jre-alpine
# image through docker.
set -euo pipefail
cd "$(dirname "$0")/../../formal/ci_red_triage"
JAR_SHA=936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88
JAR_URL=https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar
MATRIX=(CiRedTriage_design M1_key_head M2_key_delivery M3_blind_insert M4_volatile_dedup
        M5_ack_before_write M6_poll_own_writer M7_no_poll M8_upsert_update
        M9_start_from_memory M10_receiver_no_dedup M11_dead_letter_unrecorded)
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
    -workers "${WORKERS:-2}" -metadir "states/$name" -cleanup CiRedTriage.tla \
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
  inv="$(grep -m1 -oE 'Invariant [A-Za-z0-9_]+ is violated' "$out" | cut -d' ' -f2 || true)"
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
  echo "model: CiRedTriage.tla sha256=$(sha CiRedTriage.tla)"
  for c in "${MATRIX[@]}"; do
    tlc "$c"
    echo "$c cfg_sha256=$(sha "$c.cfg") out_sha256=$(sha "results/$c.out") result=$(verdict "$c")"
  done
} > results/SUMMARY.txt.new
mv results/SUMMARY.txt.new results/SUMMARY.txt
cat results/SUMMARY.txt
