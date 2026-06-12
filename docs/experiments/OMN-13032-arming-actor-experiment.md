# OMN-13032: Arming-Actor Experiment — GITHUB_TOKEN vs PAT Self-Enqueue

**Status**: EXPERIMENT — controlled test only. This PR must NOT be merged.

## Purpose

Controlled experiment: arm this sacrificial PR via GITHUB_TOKEN (the default
GitHub Actions token used by the auto-merge workflow) and record whether
`ADDED_TO_MERGE_QUEUE_EVENT` appears in the PR timeline.

This tests the "armed != enqueued" hypothesis (Current State #19,
TOTAL_ARCHITECTURE_FINDINGS.md Appendix A, 0-for-7 self-enqueueing record on
omnimarket). The question: does the token type determine whether arming
auto-merge triggers a queue entry?

## Experiment design

1. Create this trivial PR on omnimarket (a queue repo)
2. Arm auto-merge via `gh pr merge --auto` using GITHUB_TOKEN
3. Probe the PR timeline for ADDED_TO_MERGE_QUEUE_EVENT
4. Record result: self-enqueue YES or NO, per token type
5. Close the PR and delete the branch

## Completion shape

RED-CASE PROOF is the completion shape. A merged PR is the WRONG outcome.
Evidence: presence or absence of ADDED_TO_MERGE_QUEUE_EVENT in timeline.

## References

- OMN-13031: armed-not-enqueued detector (sweep tooling)
- OMN-13032: this arming-actor experiment
- DISPATCH_TEMPLATE.md §G: "Armed != enqueued"
