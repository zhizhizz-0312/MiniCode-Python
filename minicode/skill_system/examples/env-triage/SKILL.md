---
name: env-triage
version: 1.0.0
description: Collect environment facts, verify interpreter and dependencies, then diagnose
triggers: [环境排障, env triage, 环境问题, 依赖冲突, 排查环境]
tools: [load_skill, run_command, read_file, grep_files, ask_user]
risk_level: low
kind: composite
---

# Environment triage

Explain why a Python project misbehaves on this machine before changing anything.

## When to use

- Import or startup errors appear only on one machine or one virtualenv.
- A dependency version conflict is suspected but not yet proven.
- The active interpreter is unclear and every command behaves differently.

## Steps

1. collect -> collect-env-info.
2. interpreter -> check-python-runtime, after collect.
3. dependencies -> check-dependencies, after collect (runs in parallel with interpreter).
4. diagnose -> diagnose-environment, after interpreter and dependencies.
5. fixes -> suggest-fixes, after diagnose, only when the diagnosis failed.

## Procedure

1. Load the referenced atomic skills and collect facts once; both checks read that snapshot.
2. Run the interpreter and dependency checks against the same collected snapshot.
3. Correlate both results into one ranked diagnosis with explicit evidence per finding.
4. Prescribe ordered, reversible fixes only when the diagnosis did not come back healthy.

## Guardrails

- Collect and diagnose only; never install, upgrade or uninstall as part of triage.
- Never print secret values found in config; report the key name and its location.
- One machine's environment is not evidence about CI or another developer's machine.
