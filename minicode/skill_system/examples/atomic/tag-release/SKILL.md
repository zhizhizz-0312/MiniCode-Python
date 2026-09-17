---
name: tag-release
version: 1.0.0
description: Create the annotated release tag and verify it points at the checked commit
triggers: [打tag, git tag, 打标签, 发布标签, tagging]
tools: [run_command, read_file, ask_user]
risk_level: high
kind: atomic
---

# Tag release

Attach one annotated tag to the exact commit that passed verification.

## When to use

- A verified release commit needs its permanent git tag.
- A tag is suspected to point at the wrong commit.
- A release is being re-cut after a rollback.

## Procedure

1. Read the version from packaging metadata and build the expected tag name from it.
2. Confirm the working tree is clean and HEAD is the commit the tests verified.
3. Check that no local or remote tag with that name already exists.
4. Create the annotated tag with a message naming the version and the verification evidence.
5. Verify the tag resolves to the expected commit and report the tag name and SHA.

## Guardrails

- Never delete, move or force-push an existing tag; ask a human instead.
- Never tag a dirty working tree or an unverified commit.
- Stop and report rather than guessing when the version and tag name disagree.
