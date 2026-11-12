# Review of Changes Since Last Commit

Baseline: `1cf6b40` (main). Reviewed 2026-11-09.

## Scope

`git diff HEAD` shows no tracked changes. The only change is one untracked directory:

- `.claude/agents/change_reviewer.md` (new, 8 lines)
- `.claude/agents/reviewer.md` (new, 8 lines)

No backend, frontend, planning, test or config files were modified, so there is no application code, test or doc-consistency impact. This review covers the two agent definitions only.

## Summary

Both files are valid and minimal, which fits the "be simple" rule. The main problem is that `change_reviewer` is too thin to work reliably. Its body is one sentence, and `git diff` alone would miss this exact change set because the files are untracked. There are a few smaller consistency issues.

## Findings

### Should fix

1. **`change_reviewer` does not say how to find the changes, and the obvious command misses untracked files.**
   `git diff` or `git diff HEAD` returns nothing for this change set because the files are untracked. The body says only "using shell commands". Name the commands:
   - `git status --short`
   - `git diff HEAD --stat` and `git diff HEAD` (tracked changes, staged and unstaged)
   - `git ls-files --others --exclude-standard`, then read each untracked file

2. **No review criteria or output format.**
   The body does not say what to check or how to structure `REVIEW.md`. Suggested criteria, taken from the project rules: correctness, root cause over workaround, test coverage, consistency with `planning/PLAN.md`, simplicity (no over-engineering or defensive code), no emojis, and uv usage. Suggested format: Scope, Summary, Findings grouped by severity, each with a file path and line. State whether the agent overwrites or appends to `REVIEW.md`. Overwrite is simplest because it always describes the current diff.

3. **Description is too generic for automatic delegation.**
   "carry out a comprehensive review of all changes since the last commit" is acceptable. The sibling `reviewer` has "when requested", which gives the parent agent no trigger. A description such as "Use after finishing an increment and before committing. Reviews uncommitted changes and writes planning/REVIEW.md" makes delegation more reliable.

### Consider

4. **Agent name style (unverified).**
   `change_reviewer` uses an underscore while `reviewer` has none. Confirm against the current Claude Code sub-agent docs whether names must be lowercase with hyphens. If so, rename to `change-reviewer`. This was not checked in this review.

5. **Overlap with `.claude/commands/doc-review.md`.**
   That command reviews a planning doc and appends a section at the end of the same file. `reviewer` instead writes a separate `REVIEW_PLAN.md`, and `change_reviewer` writes `REVIEW.md`. Pick one convention. Opportunity to simplify: make `reviewer` a thin agent, or drop it in favour of `/doc-review PLAN.md`. `planning/archive/MARKET_DATA_REVIEW.md` is a separate review file, so separate review files already have precedent.

6. **`reviewer` is hardcoded to `planning/PLAN.md`.**
   It cannot review other planning docs such as `MARKET_DATA_SUMMARY.md`. Taking the file from the caller's prompt would make it reusable.

7. **Reviewer agent has no read-only guard.**
   `change_reviewer` has unrestricted `Bash` plus `Write`. A reviewer should not modify source. Add one line to the body ("Never modify files other than planning/REVIEW.md"). If the current docs support scoping, restrict `Bash` to git read commands.

### Minor

8. `reviewer.md` has no trailing newline. Harmless.
9. `model: sonnet` is an alias, so it will drift as models change. This is fine if intended.
10. Output files are not documented. `planning/REVIEW.md` and `planning/REVIEW_PLAN.md` are not mentioned in `PLAN.md`, `CLAUDE.md` or the README. Decide whether they are committed artifacts (as the archived review was) or should be gitignored, and add the rule to `.gitignore` if the latter. A one-line mention in the planning docs is enough, since PLAN.md says agents interact through files in `planning/`.
11. The `.claude/agents/` directory is untracked. The other `.claude` assets (`commands/`, `skills/`, `settings.json`) are committed, so add these too once the issues above are settled.

## Suggested `change_reviewer.md` body

```
Review every change since the last commit and write your findings to planning/REVIEW.md, overwriting it.

1. Find changes: `git status --short`, `git diff HEAD --stat`, `git diff HEAD`, and
   `git ls-files --others --exclude-standard` (read untracked files directly).
2. Check against planning/PLAN.md and CLAUDE.md: correctness, root cause not workaround,
   tests, simplicity (no over-engineering), consistency with docs, no emojis, uv usage.
3. Write REVIEW.md with sections: Scope, Summary, Findings (Should fix / Consider / Minor,
   each with file:line).

Do not modify any file other than planning/REVIEW.md.
```
