---
name: scan-secrets
version: 1.0.0
description: Scan changed files and hunks for credentials, keys and private endpoints
triggers: [密钥扫描, secret scan, 敏感信息, 泄漏, 凭据]
tools: [grep_files, read_file, run_command]
risk_level: medium
kind: atomic
---

# Scan secrets

Find credentials that a change would publish before the change is shared.

## When to use

- A diff or file set is about to be committed, pushed or published.
- A configuration file changed and may now contain a live value.
- A secret leak is suspected after a paste, log or error report.

## Procedure

1. Build the scan set: changed files, plus added lines of the diff.
2. Grep for known credential shapes: API keys, tokens, private key headers, connection URLs.
3. Grep for high-entropy assignments to names containing key, token, secret or password.
4. Read each hit in context and classify it as a real secret, a placeholder, or a false positive.
5. Report the file, line, key name and finding type, and mark any real secret as blocking.

## Guardrails

- Never print, echo or copy the secret value itself anywhere in the report.
- Never rotate or revoke credentials automatically; ask a human first.
- A test fixture that only looks like a key is reported as a false positive, not hidden.
