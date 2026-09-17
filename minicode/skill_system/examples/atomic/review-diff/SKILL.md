---
name: review-diff
version: 1.0.0
description: Review the collected diff for correctness, regressions and missing tests
triggers: [评审diff, review code, 代码审查, 找bug, 回归风险]
tools: [read_file, grep_files, run_command]
risk_level: medium
kind: atomic
---

# Review diff

Read a diff the way a reviewer must: prove the change does what it claims.

## When to use

- A collected diff is ready and needs a verdict with evidence.
- A change touches a risky area and deserves a second, structured pass.
- A commit message claims behavior that the diff may not actually implement.

## Procedure

1. Restate the change's intent in one sentence and confirm the diff matches it.
2. Read each hunk in file order and check control flow, off-by-one and error handling.
3. Grep for the callers of every changed public function to find broken call sites.
4. Check that new behavior is covered by a test and that removed tests are justified.
5. Classify each finding as blocking, important or nit with file and line evidence.
6. Decide pass or fail; any blocking finding means fail.

## Guardrails

- Never edit the reviewed code; report findings and let the author fix them.
- Never report a finding without a concrete file and line reference.
- Unknown behavior is a question to ask, not a defect to assume.
