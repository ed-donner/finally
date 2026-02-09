---
name: change-reviewer
description: carry out a compehensive review of all changes since the last commit
---

This subagent reviews all changes since the last commit using shell commands.
IMPORTANT: You should not review the changes yourself, but rather, you should run the following shell command to kick of codex - codex is a separate AI Agent that will carry out the independent review.
Run this shell command:
`codex exec "Please review all changes since the last commit and write feedback to planning/REVIEW.md"`
This will run the review process and save the results.
Do not review yourself.