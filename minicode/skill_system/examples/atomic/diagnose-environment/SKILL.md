---
name: diagnose-environment
version: 1.0.0
description: Correlate collected environment facts into one ranked root-cause diagnosis
triggers: [环境诊断, diagnose, 根因分析, 排障, root cause]
tools: [read_file, grep_files, run_command, load_skill]
risk_level: low
kind: atomic
---

# Diagnose environment

Reduce several check results to the one cause that explains them all.

## When to use

- The environment checks finished and their combined meaning is still unclear.
- Several symptoms probably share a single root cause.
- A fix is about to be proposed and must be justified first.

## Procedure

1. Restate each check result as a fact with the evidence that produced it.
2. Group facts that could share one cause, for example a wrong interpreter explaining both
   missing packages and import failures.
3. Rank candidate causes by how many facts each explains, then by how cheap it is to test.
4. Name the leading cause and state what evidence would falsify it.
5. Mark the diagnosis healthy, degraded or failed, and list the facts left unexplained.

## Guardrails

- Diagnose only; never apply a fix in this step, even an obvious one.
- Never present a hypothesis as a confirmed cause without supporting evidence.
- Never blame user error; report the observed state and the mismatch it creates.
