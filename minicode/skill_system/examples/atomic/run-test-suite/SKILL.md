---
name: run-test-suite
version: 1.0.0
description: Run the project test suite with sane flags and report the failing cases
triggers: [跑测试, run tests, 单元测试, pytest, 回归测试]
tools: [run_command, read_file, grep_files]
risk_level: medium
kind: atomic
---

# Run test suite

Produce one unambiguous green or red signal for the current working tree.

## When to use

- A release, tag or merge is gated on the suite being green.
- A change is claimed safe and needs evidence before it is trusted.
- A previously fixed failure may have regressed.

## Procedure

1. Detect the runner from the project files (`pytest.ini`, `tox.ini`, `pyproject.toml`) rather than assuming one.
2. Run the suite with a fixed seed and short traceback so failures stay readable.
3. Capture the exit code, the summary line and the first failing test per file.
4. Re-run only the failing tests once to separate real failures from flakes.
5. Report green or red plus the exact command that produced the result.

## Guardrails

- Never edit source or tests to make the suite pass; report the failure instead.
- Never treat a timeout or an import error as a pass.
- Do not run tests that require network or paid credentials without asking first.
