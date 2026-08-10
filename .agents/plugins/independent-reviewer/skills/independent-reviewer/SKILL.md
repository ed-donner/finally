---
name: independent-reviewer
description: Carry out an independent review of all changes since last commit
---

# Independent Reviewer Skill

Perform an impartial, comprehensive code review of all changes introduced since the last commit.

## Execution Steps

1. Inspect modified and untracked files using `git status` and `git diff HEAD~1`.
2. Evaluate:
   - **Correctness & Architecture**: Verify design patterns, logic, and state handling.
   - **Quality & Style**: Check formatting, linting rules, and comments.
   - **Performance & Security**: Identify potential memory leaks, redundant API calls, or safety risks.
3. Write actionable feedback and review summary to `planning/REVIEW.md`.
