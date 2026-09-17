---
name: summarize-review
version: 1.0.0
description: Write the review report with either a merge verdict or a changes list
triggers: [评审汇总, review summary, 评审意见, 结论, 总结]
tools: [write_file, read_file, ask_user]
risk_level: low
kind: atomic
---

# Summarize review

Turn raw review findings into one report a human can act on immediately.

## When to use

- A review and a secret scan have both finished and need one conclusion.
- A pull request needs a written verdict rather than inline comments.
- The same findings must be reported to an author and to a release owner.

## Procedure

1. Merge the review findings and the secret scan results, de-duplicating by file and line.
2. Count blockers by category: correctness, regression, test gap, exposed credential.
3. If any blocker exists, write the required-changes list ordered by severity.
4. Otherwise write the merge verdict, naming the reviewed ref and the evidence used.
5. Keep every claim tied to a file and line, and state explicitly what was not reviewed.
6. Save the report to the requested path and print the same text to the reviewer.

## Guardrails

- Never soften or omit a blocking finding to reach a positive verdict.
- Never include a secret value in the report; reference the key name and location only.
- Absence of findings is not proof of correctness; state the review's limits.
