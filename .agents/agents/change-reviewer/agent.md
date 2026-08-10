---
name: change-reviewer
description: carry out a comprehensive review of all changes since the last commit using Gemini 3.6 Flash
model: gemini-3.6-flash
tools:
  - run_command
  - view_file
  - write_to_file
---

This subagent reviews all changes since the last commit using shell commands.
IMPORTANT: Execute the following shell command to carry out the review using agy CLI:
`agy exec --model gemini-3.6-flash "Please review all changes since the last commit and write feedback to planning/REVIEW.md"`
This will run the review process and save the results.
