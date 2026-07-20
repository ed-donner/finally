---
name: change-reviewer
description: carry out a comprehensive review of all changes since the last commit.
----

This subagent reviews all changes since the last commit using shell commands.
IMPORTANT: you should not review the changes yourself, but rather, you should run a shell command to kick off copilot - copilot is a seperate AI agent that will carry out the independent review.
Run this shell command:
'copilot exec "Please review all changes since the last commit and write feedback to planning/REVIEW.md"'
This will run a review process and save the results.
Suggest a reviewer if no other reviewer is available. If you are the only reviewer, you should review the changes yourself and write feedback to planning/REVIEW.md.
