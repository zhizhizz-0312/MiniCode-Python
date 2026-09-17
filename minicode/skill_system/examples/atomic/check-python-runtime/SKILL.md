---
name: check-python-runtime
version: 1.0.0
description: Verify the active interpreter, virtualenv and supported Python version range
triggers: [python版本, interpreter, 解释器, 虚拟环境, venv]
tools: [run_command, read_file, web_search]
risk_level: low
kind: atomic
---

# Check Python runtime

Decide whether the interpreter actually running the code is a supported one.

## When to use

- An import error may come from the wrong interpreter rather than missing code.
- The declared `requires-python` and the running interpreter may disagree.
- A tool works in one shell and fails in another.

## Procedure

1. Resolve the interpreter that the failing command really uses, not the one on `PATH`.
2. Compare its version against `requires-python` and report the comparison explicitly.
3. Check whether a virtualenv is active and whether the base prefix is a system install.
4. Verify that `pip` and `python` belong to the same environment.
5. Report healthy or not healthy, naming each mismatch and the interpreter path.

## Guardrails

- Never install or switch interpreters, and never edit `PATH` or activation scripts.
- A version mismatch is a finding, not permission to upgrade anything.
- Report the interpreter path with every finding; version strings alone are ambiguous.
