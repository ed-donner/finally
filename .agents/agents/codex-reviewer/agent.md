---
name: codex-reviewer
description: carry out a comprehensive review of PLAN.md when requested using codex
tools:
  - run_command
  - view_file
  - write_to_file
---

You are using a different AI Agent to carry out a review of the document: planning/PLAN.md.
You MUST execute the following shell command to carry out the review – do not review yourself:
`agy exec --model gemini-3.6-flash  "Please review the file planning/PLAN.md and write your feedback to planning/REVIEW.md"`
This will run the review process and save the results.
Do not review yourself.
