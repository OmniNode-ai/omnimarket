Durable evidence delivery model (OMN-20071)
============================================

This is the OR.3 model-before-build preparation for the `omnimarket` verifier
and verdict projection. It is not an artifact-storage adapter, a retention
policy, or a merge gate. OR.2 remains a prerequisite for that implementation.

The service owner for storage, retention/deletion, access controls, and restore
is Jonah/operator, per the operator statement on 2026-09-29. The later typed
storage contract must name the backend, retention/deletion rules, access scope,
and restore procedure. This model intentionally does not choose any of them.

Modelled facts
--------------

* An intent allocates an attempt sequence off-host and binds it to the
  candidate subject and the contract revision before dispatch. A bounded
  foreign subject proves this equality is a checked condition, not a
  tautology; production identity stays an opaque typed tuple.
* A local PASS is not admitted; only a durably confirmed PASS can be admitted.
* A local PASS lost with the executing machine before confirmation becomes an
  explicit UNRESOLVED lost attempt. It cannot be confirmed or admitted, and
  the off-host allocation remains recorded.
* A confirmation can be redelivered without creating a second logical attempt.
* A revised contract prevents an earlier revision's PASS from admission, and
  a later allocated pending, cancelled, or lost attempt suppresses an earlier
  PASS: admission selects the highest allocated sequence.
* Cleanup is modelled as an operation and preserves recorded evidence.

The model is bounded to two contract revisions and two allocated attempts. That
is enough to reach the stale-revision, highest-allocation, and redelivery
counterexamples; it does not claim liveness, a storage implementation, or
policy approval.

Check matrix
------------

Config                         Single removed guard         Required violation
M1_no_off_host_intent          off-host allocation           DispatchUsesRecordedIntent
M2_dispatch_stale_intent       exact subject/revision intent  DispatchUsesExactCurrentIntent
M2_admit_unconfirmed           durable confirmation           NoAdmissionWithoutConfirmation
M3_nonidempotent_confirmation  confirmation idempotency       OneLogicalAttemptPerExecution
M4_admit_stale_revision        current revision binding       NoStaleRevisionAdmission
M5_cleanup_drops_history       evidence retention             RecordedEvidenceIsRetained
M6_admit_older_pass            highest allocated sequence     HighestAllocatedAttemptSelected
M7_lost_unconfirmed_pass       local PASS disappears          LostUnconfirmedPassIsUnresolved
M8_dispatch_foreign_subject    exact subject binding          DispatchUsesExactSubjectIntent

Run `TLA2TOOLS_JAR=/path/to/tla2tools.jar TLA2TOOLS_SOURCE=https://... \
./run_all.sh`. The runner does not download tools. It records the supplied
source plus SHA-1 and SHA-256 in `results/SUMMARY.txt`, accepts only a zero-exit
design run containing TLC's design success text and a nonzero run containing
the named invariant violation for each mutant, and fails on tool errors.

The checked proof used the official TLA+ v1.8.0 release artifact at
`https://github.com/tlaplus/tlaplus/releases/download/v1.8.0/tla2tools.jar`.
Its published SHA-1 and observed SHA-1 are
`2b8c20402dc740fed5b03f9d39f652e85d70b17c`; observed SHA-256 is
`ab4694601923fd5ac06452abbf847c366a5054a3d739552085edd6ed986c29ec`.
