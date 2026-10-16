## Acceptance criteria

- [ ] AC3: The S3 context is required on omnimarket while every OCC context stays required, and a difference between the two paths that has no reason code in the OR.1 expected-difference table fails the check and names the difference -- falsifier: a ruleset readback on omnimarket lacks the S3 context or any OCC context, or a difference with no reason code passes the check
- [ ] AC4: 30 eligible omnimarket PRs pass through both paths with no unclassified difference, no new-path admit where OCC refused for a behavioral reason, and no accepted negative control -- falsifier: the pilot report lists any of those three, or counts fewer than 30 eligible PRs
- [ ] AC5: The addendum section 7 cases C1, C2, C4, C5, C8, C10, C13, C14, C15, C16, C17 and C18, and C12 observed under the landing controller's relief, each have an observation -- falsifier: the pilot report has no observation for any listed case
- [ ] AC6: Rollback is one ruleset change, drilled once before the count starts -- falsifier: no drill record precedes the first counted PR, or the drill's ruleset readback differs from the pre-pilot required-context set

## Superseded 2026-10-02

+++ Superseded acceptance criteria (OR.4 shadow-pilot exits, replaced by plan step S5)

* (superseded) AC1: The shadow positive control completes, every case in plan section 7 refuses where it should, and the window meets plan section 8 -- falsifier: the shadow report for the window lists an unclassified difference, a new-path admit where OCC refused for a behavioral reason, or a plan section 7 case with no observation
* (superseded) AC2: The shadow check is report-only -- falsifier: a ruleset or required-contexts readback on the pilot repository that lists the shadow check as required

+++
