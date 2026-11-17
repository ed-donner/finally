---
name: doc-review
description: >-
  Review a documentation file in the planning/ folder against the rest of the
  project and codebase, and append a structured "Review & Open Questions"
  section at the end. Use when the user asks to review or run doc-review on a
  planning doc.
argument-hint: <file-in-planning-folder> (e.g. PLAN.md)
---

# Documentation Review

Review the documentation file `$ARGUMENTS` (or the file requested by the user) in the `planning/` directory (resolve with or without the `planning/` prefix or `.md` extension; if no file is specified, ask which file in `planning/` to review).

Cross-check the document against other files in `planning/` and the current codebase, then append (or update if already present) a `## Review & Open Questions` section at the very end of the file. **Do not modify any existing sections above it.**

Structure the review with concise, actionable bullets under:
- **Gaps & Ambiguities**: Missing specs, edge cases, or unclear contracts between components/agents.
- **Inconsistencies**: Conflicts with other docs in `planning/` or the existing implementation.
- **Opportunities to Simplify**: Over-engineered details or scope that can be streamlined.
- **Open Questions**: Concrete decisions needed before or during implementation.
