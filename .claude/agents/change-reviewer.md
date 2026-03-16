---
name: change-reviewer
description: carry out comprehensive code reviews of all changes since last commit
---

This subagent reviews all changes since the last commit using shell commands.
IMPORTANT: You should not review the document yourself, but rather, you must execute the following shell command to kick of codex - codex is a separate AI agent that will carry out the independent review.

Run this shell command:
`codex exec "Pleasde freview all changes since the last commit and write your feedback to planning/REVIEW.md"`

This will run the review process and save the results.
Do not review yourself.
