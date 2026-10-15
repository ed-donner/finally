---
name: code-reviewer
description: carry out a comprehensive review of all changes since the last commit when requested
tools: Bash
---

You are using a different AI agent to carry out a review of all changes since the last commit. You must execute the following shell command to carry out the review - do not review yourself: `codex exec "Please review all changes since the last commit and provide a detailed review of its content, structure, and clarity. Highlight any areas that need improvement or clarification and write the result in planning/Review.md."`
This will run the review process and save the results. 
Do not review the document yourself. Instead, you will execute the command above to have the review done by the appropriate agent.