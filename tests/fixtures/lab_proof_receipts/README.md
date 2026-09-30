# lab_proof_receipts fixtures (OMN-19566)

`omnibase_infra-4217-event.json` is the `lab-proof-receipt` event the omnibase_infra
lab pool driver minted for its first real pr-head receipt: omnibase_infra#4217 at head
`2a626f37f3708b0661b373150af1af6219c3b6ea`, proved on lab-101 on 2026-09-28 from
12:06:40Z to 12:16:58Z (a run without a base control, so FAIL with `RESULT_NOT_PASS`).
Two fields are normalized to the form the driver now publishes: `host` is the pool
member's name, not its LAN address, and `verifier_identity` is the driver's hashed
machine identity, not the machine's name. Every other byte is as minted.
