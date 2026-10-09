<!-- prompt:KB_ROOT -->
ROOT: @@kbResolution@@
<!-- end:KB_ROOT -->
<!-- prompt:KB_WORKTREE -->
WORKTREE: @@WORKTREE_TICKET@@ on @@KB_BRANCH@@
<!-- end:KB_WORKTREE -->
<!-- prompt:KB_COMMIT -->
COMMIT path-scoped on @@KB_BRANCH@@
<!-- end:KB_COMMIT -->
<!-- prompt:DOC_RESOLUTION -->
DOCS:
- @@sourceDocs@@
<!-- end:DOC_RESOLUTION -->
<!-- prompt:COMMON -->
COMMON: ledger @@LEDGER_PATH@@, peer claim window @@PEER_CLAIM_MINUTES@@ minutes. @@fences@@
<!-- end:COMMON -->
<!-- prompt:DELEGATION_STEP -->
DELEGATE prose through the lab.
<!-- end:DELEGATION_STEP -->
<!-- prompt:PRECHECK_BRIEF -->
PRECHECK for @@date@@:
@@precheckPhaseLines@@
<!-- end:PRECHECK_BRIEF -->
<!-- prompt:idempotency-precheck -->
@@PRECHECK_BRIEF@@
<!-- end:idempotency-precheck -->
<!-- prompt:decisions-register -->
@@COMMON@@
DECISIONS to @@DECISIONS_MD_PATH@@ and @@DECISIONS_JSON_PATH@@, stale after @@DECISIONS_STALE_DAYS@@ days.
<!-- end:decisions-register -->
<!-- prompt:ground-state -->
@@KB_ROOT@@
@@KB_WORKTREE@@
@@DOC_RESOLUTION@@
Write @@GROUND_STATE_PATH@@ against @@SNAPSHOT_PATH@@. Probe @@closureProbeTicket@@ within @@closureProbeCeilingSeconds@@s. Repos: @@integrationRepos.join(', ')@@.
<!-- end:ground-state -->
<!-- prompt:morning-triage -->
@@COMMON@@
Triage for @@date@@. @@KB_COMMIT@@
<!-- end:morning-triage -->
<!-- prompt:plan-reconcile -->
@@COMMON@@
@@upstreamNote('GroundState', g, GROUND_STATE_PATH)@@
@@upstreamNoteTriage@@
Write @@REBASELINE_PATH@@.
<!-- end:plan-reconcile -->
<!-- prompt:integration-plan -->
@@COMMON@@
@@upstreamNote('Reconcile', reconcile, REBASELINE_PATH)@@
Since @@integrateSince@@; write @@INTEGRATION_PATH@@.
<!-- end:integration-plan -->
<!-- prompt:dropped-work -->
@@COMMON@@
@@upstreamNote('Integrate', integrate, INTEGRATION_PATH)@@
Sections: @@DROPPED_SECTIONS[0]@@, @@DROPPED_SECTIONS[1]@@, @@DROPPED_SECTIONS[2]@@, @@DROPPED_SECTIONS[3]@@, @@DROPPED_SECTIONS[4]@@. Headline keys: @@DROPPED_HEADLINE_KEYS.join(', ')@@ (@@DROPPED_HEADLINE_KEYS[0]@@, @@DROPPED_HEADLINE_KEYS[1]@@, @@DROPPED_HEADLINE_KEYS[2]@@, @@DROPPED_HEADLINE_KEYS[3]@@, @@DROPPED_HEADLINE_KEYS[4]@@, @@DROPPED_HEADLINE_KEYS[5]@@). Write @@DROPPED_WORK_PATH@@.
<!-- end:dropped-work -->
<!-- prompt:session-goal -->
@@COMMON@@
@@upstreamNote('DroppedWork', droppedWork, DROPPED_WORK_PATH)@@
@@publish ? 'PUBLISH' : 'DRY'@@
Write @@GOAL_PATH@@.
<!-- end:session-goal -->
