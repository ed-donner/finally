# Review: root README.md

Reviewed 2026-11-09 against the working tree and `planning/PLAN.md`. Baseline commit `1cf6b40`.

## Scope and a blocking observation

`README.md` does not exist in the working tree. `git status` shows ` D README.md` (tracked, deleted). The only candidate is the untracked `README_v1.md`. Compared with `git show HEAD:README.md` it is identical except for line endings (CRLF vs LF), so it is not a new README in content. This review covers `README_v1.md` and assumes it is the intended replacement.

Other uncommitted changes (`.claude/settings.json` plugin entry, `.claude-plugin/`, `.claude/agents/`, `independent-reviewer/`, `planning/REVIEW_v1.md`) are unrelated to the README and were not reviewed.

## Verdict

Length is fine: 75 lines, about 420 words. Env vars, the `docker run` command, the stack and the port all match `PLAN.md` and `.env.example`. The main problem is that the README presents the project as runnable, but only the market data backend exists.

## Findings

### Must fix

1. **Wrong file name and state.** `README.md` is deleted and the content sits in untracked `README_v1.md` with CRLF endings. Rename to `README.md` and save with LF, otherwise the diff against the committed file shows every line changed. `backend/pyproject.toml` also declares `readme = "README.md"` relative to `backend/`, which is unaffected, but the root file must be `README.md`.

2. **No status, and Quick Start cannot work today.** The README never says what is built. Verified in the repo:
   - Present: `backend/app/market/` (simulator, Massive client, cache, SSE router factory), `backend/tests/market/`, `planning/`, `.env.example`, `LICENSE`.
   - Missing: `Dockerfile`, `scripts/` (all four start/stop scripts), `frontend/`, `test/`, `db/`.
   - Also missing in the backend: `app/main.py`, `/api/health`, any use of `create_stream_router` outside its own package, and an `anthropic` dependency in `backend/pyproject.toml`.

   So `scripts/start_mac.sh` and `docker build -t finally .` fail. Fix: add a short "Status" section near the top, for example "Market data backend is complete (see `planning/MARKET_DATA_SUMMARY.md`). API, frontend, chat and Docker are not built yet." Either mark Quick Start as "planned" or move it under that heading.

3. **Project Structure lists directories that do not exist.** `frontend/`, `test/`, `db/` and `scripts/` are listed as if present. Either label them "(planned)" or list only what exists. Suggested honest tree: `backend/`, `planning/`, `.env.example`, `LICENSE`, plus planned dirs marked as such.

4. **Local Development is a stub that skips what works now.** PLAN.md line 401 says to "document the setup in the README". The section gives no commands. Add the commands that work today:
   ```bash
   cd backend
   uv sync --extra dev
   uv run pytest
   uv run market_data_demo.py   # terminal price dashboard
   ```
   Leave the Next.js rewrite and uvicorn commands for when those exist.

### Should fix

5. **`uv sync --dev` does not install pytest here (proven).** In `backend/README.md` (and its `uv sync --dev` lines), dev tools are an optional extra (`[project.optional-dependencies] dev`), not a dependency group. I tested in a temp copy of `pyproject.toml` and `uv.lock`. After `uv sync --dev --locked`, `uv run pytest` fails with "program not found". After `uv sync --extra dev --locked`, it gives `pytest 9.0.2`. `backend/CLAUDE.md` already uses the correct `--extra dev`. Fix `backend/README.md` to match, and use `--extra dev` in the root README. Out of scope for this review to edit, so flagged only.

6. **Start-script flags are incomplete.** PLAN.md sections 11 and 11a give `--build` and `--open` for the shell script and `-Build` and `-Open` for PowerShell. The README mentions only `--build`, and nothing for Windows. Fix: one line, "Flags: `--build`, `--open` (PowerShell: `-Build`, `-Open`)".

7. **Features list is written in present tense.** Everything in "Features" is planned except the simulator and the SSE router factory. The status note in item 2 covers this, or retitle the section "Planned features". It also duplicates the Architecture section and PLAN.md section 2, so it is the best candidate to trim if conciseness matters.

### Consider

8. **No pointer to the docs.** Add one line linking `planning/PLAN.md` (spec) and `planning/MARKET_DATA_SUMMARY.md` (completed work). CLAUDE.md treats these as the source of truth.

9. **Env table could state defaults.** `LLM_MOCK` defaults to `false`, and `MASSIVE_API_KEY` empty means simulator. Both are in `.env.example` and PLAN.md section 5. Optional, one short column or clause. The table content itself is correct.

10. **Minor wording.**
    - The "Clone and configure" comment has no clone command. Either add `git clone` or reword to "Configure".
    - License line could say "MIT" (the `LICENSE` file is MIT, Ed Donner 2026).
    - "visually stunning" is marketing language copied from PLAN.md. Drop it if shortening.

## Claims verified as correct

| Claim | Evidence |
|---|---|
| Port 8000, single container | PLAN.md sections 3 and 11 |
| `docker run -v finally-data:/app/db -p 8000:8000 --env-file .env finally` | Identical to PLAN.md section 11 |
| `cp .env.example .env` | `.env.example` exists; `.env` is gitignored (`.gitignore` line 138) |
| Five env vars and their roles | `.env.example`, PLAN.md section 5; `MASSIVE_API_KEY` read in `backend/app/market/factory.py` |
| Stack: Next.js static export, TypeScript, Tailwind, FastAPI/uv, SQLite, Anthropic SDK structured outputs, GBM or Massive | PLAN.md sections 3, 9, 10 |
| Stop keeps the volume | PLAN.md section 11 |
| Start/stop script names (`start_mac.sh`, `stop_mac.sh`, `start_windows.ps1`, `stop_windows.ps1`) | PLAN.md section 4 (names correct, files not yet created) |
| LICENSE link | `LICENSE` exists |

## Suggested minimal edit order

1. Rename `README_v1.md` to `README.md`, LF endings.
2. Add the Status section and mark planned items (findings 2, 3, 7).
3. Replace Local Development with the working backend commands (finding 4).
4. Add the flags line and doc links (findings 6, 8).
5. Fix `uv sync --dev` in `backend/README.md` (finding 5).
