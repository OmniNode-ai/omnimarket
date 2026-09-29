#!/usr/bin/env bash
# Runs the whole matrix and writes results/SUMMARY.txt with the model's content digest.
set -euo pipefail
cd "$(dirname "$0")"
digest="$(shasum -a 256 DevheadGuard.tla | cut -d' ' -f1)"
{
  echo "model: DevheadGuard.tla sha256=$digest"
  for c in DevheadGuard_design DevheadGuard_design_flaky M1_p1_no_idempotency M2_p2_self_only \
           M3_p3_no_gate M4_p4_no_release_on_crash M5_p5_no_deadline; do
    ./run_tlc.sh "$c"
    if grep -q "No error has been found" "results/$c.out"; then r=PASS
    else r="VIOLATED $(grep -m1 -oE 'Invariant [A-Za-z0-9_]+ is violated' "results/$c.out" | cut -d' ' -f2)"; fi
    echo "$c cfg_sha256=$(shasum -a 256 "$c.cfg" | cut -d' ' -f1) result=$r"
  done
} | tee results/SUMMARY.txt
