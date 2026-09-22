# Security policy

## Data access

The watcher reads local Codex JSONL session files to find session metadata and
completed-turn error events. It does not upload transcripts or add a network
client. The selected `codex` executable is responsible for communicating with
the local app-server and the normal Codex service.

## Reporting a vulnerability

Please open a GitHub issue without including access tokens, private prompts,
session transcripts, repository secrets, or other sensitive data. If a report
requires sensitive reproduction details, first open a minimal issue asking
the maintainer for a private reporting channel.
