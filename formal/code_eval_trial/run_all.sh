#!/usr/bin/env bash
# Runs the design configs and every mutation; each design must pass and each mutation must fail.
set -euo pipefail
cd "$(dirname "$0")"
for cfg in CodeEvalTrial_design CodeEvalTrial_timed CodeEvalTrial_liveness CodeEvalTrial_liveness_normal \
  M1_close_with_try_pending M2_late_answer_changes_row M2b_unknown_message_changes_row \
  M3_place_before_control M4_concurrent_scores M5_no_nudge_expiry M6_bound_below_timeout \
  M6b_bound_equals_deadline_minus_one M7_no_fairness M8_drop_ordinal_guard M9_continue_after_pass \
  M10_no_nudge_expiry_liveness; do
  ./run_tlc.sh "$cfg"
done
echo "--- clean (No error has been found):"
grep -l "No error has been found" results/*.out
echo "--- violated (each mutation M* must be listed here, no design config):"
grep -L "No error has been found" results/*.out
