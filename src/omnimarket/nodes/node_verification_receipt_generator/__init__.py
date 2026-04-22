# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_verification_receipt_generator — generates evidence receipts for task-completed claims.

Input: ModelVerificationReceiptRequest (task claim + PR refs)
Output: ModelVerificationReceipt (gh pr checks conclusions + pytest exit code)

Feeds overseer-verify with real evidence — kills rubber-stamping at the source.
OMN-9403.
"""
