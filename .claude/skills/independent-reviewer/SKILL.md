---
name: independent-reviewer
description: Carry out an independent review of all changes since the last commit by delegating to Codex. Use when the user wants a code review of recent uncommitted work.
disable-model-invocation: true
allowed-tools: Bash(git *), Bash(codex *)
---

# Independent Reviewer

Delegate a thorough code review of all uncommitted changes to Codex.

## Steps

1. Run `git diff --stat` to get a summary of what changed
2. Run `git diff --name-only` combined with `git diff --cached --name-only` to build the full list of changed files
3. Build a detailed Codex prompt (see below) and execute it via `codex exec`

## Building the Codex Prompt

Construct a single `codex exec` command with a prompt that includes:

- The list of changed files you discovered
- Clear instructions for what to review and what to write

Use this exact pattern:

```bash
codex exec "You are an independent code reviewer. Review all changes since the last commit in this repository.

Changed files: {paste the file list here}

For each changed file:
1. Run git diff on the file to see exactly what changed
2. Read the full file for context around the changes
3. Evaluate the changes for:
   - Correctness: logic errors, off-by-one bugs, race conditions, null/undefined risks
   - Security: injection vulnerabilities, exposed secrets, unsafe inputs
   - Code quality: naming clarity, duplication, overly complex logic, dead code
   - Project conventions: adherence to the patterns in CLAUDE.md (use uv not pip, no emojis, short modules, clear naming, no defensive overengineering)
   - Missing edge cases: error paths, boundary conditions, empty/null inputs

Write your complete review to planning/REVIEW.md in this format:

# Code Review

**Date:** $(date +%Y-%m-%d)
**Scope:** Changes since last commit
**Changed files:** {list them}

## Summary
2-3 sentences on what the changes accomplish.

## Findings

For each finding use this format:
### [Critical | Warning | Suggestion] - Short title
**File:** path/to/file
**Lines:** relevant line range
Description of the issue and a concrete recommended fix.

## What Looks Good
Note things that are well done.

## Overall Assessment
State whether the changes are ready to commit, or what must be addressed first."
```

## Important

- Always gather the changed file list FIRST so Codex knows exactly where to look
- Pass the file list directly into the prompt string
- Let Codex do all the reading and reviewing — do not duplicate that work yourself
- If there are no changes (git diff is empty and no staged changes), tell the user there is nothing to review instead of calling Codex
