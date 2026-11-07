# Recorded FRICTION rows of the ledger window 2026-11-06T15:44Z to 2026-11-07T15:44Z that carried no class cell and matched none of
# the 28 rules shipped by OMN-22879 (50 rows unclassified under the rules on the base branch; 48 are named by the 22 rules of the
# OMN-22865 and OMN-22879 commits already under this change; these two are the rest). Lane ids are replaced by synthetic ones; cells
# other than lane, ticket, cost, existing and the closing text are dropped; private repository names are scrubbed.

- 2026-11-07T13:38:00Z | FRICTION | lane=lane-71 | ticket=OMN-22993 | cost=25m | A bind of the drive over a host's live RAM filesystem (lanes running, TMPDIR and run paths on it) at 13:15Z ended three landing workers (no_result_file engine_rc=86): their temp files were on the RAM side, and the bind of the RAM filesystem at a drain path propagated the drive onto it (shared root mount) so links into it looped. Fixed in the install: it refuses while fuser reads the RAM filesystem in use. The controller re-dispatches <private-repo>#5221, #5240 and #5245.
- 2026-11-07T14:47:15Z | FRICTION | lane=lane-72 | ticket=OMN-22989 | existing=OMN-22989 | est ~0.5 lane-hours; the ticket's AC2 falsifier names a new test file in <private-repo>, which the src-freeze gate refuses as an added code file; the lane renamed an existing data test to get it past the gate
