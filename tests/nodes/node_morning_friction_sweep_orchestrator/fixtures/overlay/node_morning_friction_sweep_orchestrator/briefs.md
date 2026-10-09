<!-- prompt:friction-precheck -->
PRECHECK @@run_date@@ state @@state_path@@ stems @@phase_stems@@ peer window @@peer_claim_minutes@@
<!-- end:friction-precheck -->
<!-- prompt:friction-scan -->
RUN @@run_date@@ window @@window@@ state @@kb_prefix@@@@state_path@@ ledger @@ledger_path@@ @@fences@@ lookback @@lookback_hours@@ tool @@watermark_tool@@ v@@watermark_schema_version@@ watcher bound @@pr_watcher_max_age_minutes@@m
<!-- end:friction-scan -->
<!-- prompt:friction-source-linear -->
RUN @@run_date@@ window @@window@@ state @@kb_prefix@@@@state_path@@ ledger @@ledger_path@@ @@fences@@ linear bound @@linear_comments_max_age_hours@@ ceiling @@occurrence_comment_ceiling@@
<!-- end:friction-source-linear -->
<!-- prompt:friction-source-checkpoints -->
RUN @@run_date@@ window @@window@@ state @@kb_prefix@@@@state_path@@ ledger @@ledger_path@@ @@fences@@ checkpoints bound @@checkpoints_max_age_hours@@
<!-- end:friction-source-checkpoints -->
<!-- prompt:friction-source-ci -->
RUN @@run_date@@ window @@window@@ state @@kb_prefix@@@@state_path@@ ledger @@ledger_path@@ @@fences@@ ci bound @@ci_lookback_max_age_hours@@ repos @@ci_repos@@
<!-- end:friction-source-ci -->
<!-- prompt:friction-source-guards -->
RUN @@run_date@@ window @@window@@ state @@kb_prefix@@@@state_path@@ ledger @@ledger_path@@ @@fences@@ guards bound @@guard_logs_max_age_hours@@
<!-- end:friction-source-guards -->
<!-- prompt:friction-synthesize -->
RUN @@run_date@@ window @@window@@ state @@kb_prefix@@@@state_path@@ ledger @@ledger_path@@ @@fences@@
scan @@scan_handoff@@ inputs @@synthesis_inputs@@
@@source_summary@@
<!-- end:friction-synthesize -->
<!-- prompt:friction-adjudicate -->
RUN @@run_date@@ window @@window@@ state @@kb_prefix@@@@state_path@@ ledger @@ledger_path@@ @@fences@@ @@adjudication_mode@@ synthesis @@synthesis_handoff@@ unknown @@sources_unknown@@ dry_run=@@dry_run@@
<!-- end:friction-adjudicate -->
<!-- prompt:friction-report -->
RUN @@run_date@@ window @@window@@ state @@kb_prefix@@@@state_path@@ ledger @@ledger_path@@ @@fences@@ @@report_mode@@ report @@report_path@@ branch @@kb_branch@@ worktree @@worktree_ticket@@ adjudication @@adjudication_handoff@@ synthesis @@synthesis_handoff@@
<!-- end:friction-report -->
