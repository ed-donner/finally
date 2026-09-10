# HI — How this remote Claude Code session worked

A short note written by Claude Code itself, from inside the session that created this file.

## The short version

You asked for a file; an agent running on a throwaway machine in the cloud wrote it,
committed it, and pushed it back to GitHub. Nothing ran on your laptop.

## The pieces

**1. A container, not your machine.**
The session runs in an isolated, ephemeral Linux container. The repo
(`dobidobi77-bot/finally`) was cloned fresh when the container started. The container
is reclaimed after inactivity, so anything worth keeping has to be committed and
pushed — the working directory is not durable storage.

**2. Context loaded up front.**
Before the first instruction, the harness injected the project's `CLAUDE.md` and the
`planning/PLAN.md` it imports. That is why the agent already knows FinAlly is a
FastAPI + Next.js trading workstation with an SSE price stream, without reading a
single file.

**3. Tools instead of a terminal for you.**
The agent acts through a fixed tool surface: `Bash`, file read/write/edit, `Grep`,
`Glob`, sub-agents, and MCP servers (GitHub, Playwright, Context7). Each call runs
under a permission mode you chose. Some tools are *deferred* — only their names are
loaded until the agent searches for and pulls in the full schema, which keeps the
prompt small.

**4. Git is the delivery mechanism.**
Work happens on a designated branch (`claude/busy-ptolemy-virivd`), never directly on
`main`. Commits carry a co-author trailer and a link back to the session. A pull
request is only opened if you ask for one.

**5. It can outlive the turn.**
The session can subscribe to PR webhooks, so CI failures and review comments wake it
back up later and it pushes fixes without you re-prompting. It can also schedule its
own check-ins.

## What this session actually did

Read the repo state, wrote this file, committed it to the feature branch, and pushed.

## Why it's useful

You can start work from a phone or a browser tab, close it, and come back to a branch
with commits on it. The tradeoffs: no access to your local environment or secrets
beyond what the container was given, and outbound network access is limited by the
environment's policy — which is exactly why the Context7 documentation server failed
to connect during this session.
