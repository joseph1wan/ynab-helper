# ADR 002: Cursor Skill for Unmatched Items

## Status

Accepted

## Context

Some Target items will not match keyword rules. An LLM can suggest categories, but embedding API keys in the tool adds complexity.

## Decision

Use a Cursor skill (`categorize-unmatched`) for unmatched line items. The skill suggests categories and regex rules; the user approves before rules are appended to `config/rules.yaml`.

## Consequences

- No LLM API key in the tool
- Rule learning is human-approved
- Review workflow spans web UI (approve splits) and Cursor chat (tune rules)
