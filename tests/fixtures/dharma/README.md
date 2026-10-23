# Dharma Evaluation Runs fixtures (OMN-20088)

Responses for `test_evaluator_dharma.py`.

- **Recorded:** `launch_201.json`, `campaign_running.json`, `campaign_completed_block.json` and
  `campaign_completed_acceptable.json` are bodies Dharma returned on 2026-10-22 to the trajectory
  evaluation handler running against the trial organization, for a synthetic `add(a, b)` task with a
  subtracting draft (BLOCK) and a correct draft (ACCEPTABLE). The organization id is replaced by
  `org_fixture`; nothing else is changed. `error_401_invalid_token.json`, `error_403_org_not_found.json`
  and `error_400_invalid_contract.json` are bodies Dharma returned the same day (the 400 to a preflight
  with an over-long `user_message`).
  `error_403_managed_runtime_requires_real_organization.json` is the body Dharma returned on
  2026-10-23 to every managed-campaign and usage request for the trial organization's token.
- **Synthetic, docs-derived:** the `synthetic_*` files. Dharma's documentation names the nested error
  envelope, 402 for credits and 429 and 503 handling, but no such response was observed.
