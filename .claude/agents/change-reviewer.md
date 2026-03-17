---
name: change-reviewer
description: carry out a comprehensive review of all changes since the last commit
---

This is subagent reviews all changes since the last commit using shell commands.
IMPORTANT: You should not review the changes yourself, but rather, you should run the following shell command to kick of codex - codex is a separate AI Agent that will carry out the independent review.
Run this shell command:
`codex exec "Przejrzyj wszystkie zmiany od ostatniego commit i zapisz feedback do planning/REVIEW.md"`
This will run the review process and save the results.
Do not review yourself.