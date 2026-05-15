# Dep-Health Sweep Proof-of-Life Evidence

This proof fixture captures the expected dep-health sweep outcomes for a clean
fixture, a fixture with findings, event emission, and projection upserts.

## Subtest Outcomes

- **A: clean_fixture**: PASS — status=clean
- **B: finding_fixture**: PASS — status=findings, findings=2, types=['MISSING_TOPIC_EDGE', 'UNTESTED_HANDLER']
- **C: event_emission**: PASS — topic='onex.evt.omnimarket.dep-health-sweep-completed.v1', events=1
- **D: projection**: PASS — rows_upserted=2

## Evidence Summary

- Findings count: 2
- Projection rows: 2
