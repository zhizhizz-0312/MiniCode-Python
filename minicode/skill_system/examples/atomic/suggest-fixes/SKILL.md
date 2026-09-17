---
name: suggest-fixes
version: 1.0.0
description: Turn a diagnosis into ordered, reversible repair commands with checks
triggers: [修复建议, suggest fixes, 解决方案, 怎么修, remediation]
tools: [read_file, edit_file, run_command, ask_user]
risk_level: medium
kind: atomic
---

# Suggest fixes

Convert a diagnosis into the smallest repair a human can review and undo.

## When to use

- A diagnosis named a root cause and the environment is not healthy.
- A user asks what to change to make the failure go away.
- A previous fix attempt did not resolve the reported symptom.

## Procedure

1. Read the diagnosis and restate the root cause in one sentence.
2. Propose the smallest change that removes the cause, preferring configuration over installs.
3. Order the fixes: reversible and local first, environment-wide changes last.
4. For each fix give the exact command or edit, what it changes, and how to undo it.
5. Give one verification command per fix that shows whether it worked.
6. Ask before applying anything that installs, uninstalls or rewrites an environment.

## Guardrails

- Never run a fix that upgrades or uninstalls packages without explicit confirmation.
- Never suggest disabling a check, test or validation to make a symptom disappear.
- Warn when a fix would diverge this machine from the project's declared requirements.
