---
name: confirm-merge-readiness
version: 1.0.0
description: Confirm the merge gate: blockers cleared, approvals present, CI green
triggers: [合并就绪, merge readiness, 可合并, 合并检查, gate]
tools: [read_file, run_command, ask_user]
risk_level: medium
kind: atomic
---

# Confirm merge readiness

State plainly whether this change may merge, and what still blocks it.

## When to use

- A review passed and the change is being proposed for merge.
- A pull request is waiting on approvals, checks or a resolved conflict.
- Someone asks whether it is safe to merge right now.

## Procedure

1. Confirm the reviewed commit is still the branch head and has not changed since review.
2. Confirm every blocking finding from the review report is resolved.
3. Check the required checks and approvals for the target branch.
4. Confirm there is no unresolved conflict and no pending required review.
5. Report ready or not ready, listing each unmet condition with its owner.

## Guardrails

- Never merge, approve, or push on the user's behalf; confirmation only.
- Never treat a stale review of an older commit as valid for the current head.
- If check status cannot be read, report it as unknown, not as passing.
