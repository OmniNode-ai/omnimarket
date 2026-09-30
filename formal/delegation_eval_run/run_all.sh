#!/usr/bin/env bash
# Runs the design config and every mutation; the design must pass and each mutation must fail.
set -euo pipefail
cd "$(dirname "$0")"
for cfg in DelegationEvalRun_design M1_run_id_from_delivery M2_writer_inserts M3_result_depends_on_delivery; do ./run_tlc.sh "$cfg"; done
grep -l "No error has been found" results/*.out
grep -L "No error has been found" results/*.out
