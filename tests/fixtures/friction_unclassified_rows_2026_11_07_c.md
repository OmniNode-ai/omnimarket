# Recorded FRICTION rows of the ledger window 2026-11-06T13:26Z to 2026-11-07T13:26Z that carried no class cell and matched none of
# the 26 rules shipped by OMN-22865 (51 rows unclassified under the six rules on the base branch; 49 are named by the 20 rules of
# OMN-22865; these two are the rest). Lane ids are replaced by synthetic ones; cells other than lane, ticket, cost, existing and the
# closing text are dropped; private repository names and host names are scrubbed.

- 2026-11-07T10:49:33Z | FRICTION | lane=lane-61 | ticket=OMN-17195 | cost=~3 lane-minutes | pr_handoff refused exit 5: onex scm pr-view --source bus live read failed for <private-repo>#1681 comments
- 2026-11-07T13:06:06Z | FRICTION | lane=lane-62 | cost=~10 lane-minutes | existing=OMN-22904 | The brief names the plan on a branch of <private-repo> as the place to read section 4.1, but on a lab host the clone holds no such branch (its origin refs stop at an earlier main) and canonical_clone_sync refresh answers NO_CLONE for it, so the lane could not read the plan text and built the join from the ticket text and the floor receipt fold's own join rules. Correct move: a lab host that carries the plan branch, or the section text in the brief.
