# Review of changes since the last commit

Reviewed against `14550e1` on 2026-10-06. Scope includes tracked working tree changes and the new marketplace, reviewer plugin, agent definition, and command files. No application source code changed.

## Findings

### [P2] Development setup does not install the test and lint dependencies

**Location:** `README.md:36`

The new development instructions use `uv sync --dev`, followed by pytest and Ruff commands. However, `backend/pyproject.toml:15–22` declares these packages in the optional dependency extra `dev`, rather than a dependency group. `--dev` selects the development dependency group; it does not select this extra. On a fresh installation, the documented setup therefore does not install pytest or Ruff, and the next commands fail.

Change the command to `uv sync --extra dev`, or migrate the dependency declarations to a development dependency group. The backend README has the same existing problem and should be aligned with the corrected root instructions.

### [P2] Root `.env` instructions do not match the current backend's loading behavior

**Location:** `README.md:22`

The README tells users to put environment variables in a root `.env` file, but the existing market data factory reads `MASSIVE_API_KEY` exclusively from `os.environ` (`backend/app/market/factory.py`). No backend application code loads the root `.env`, and the documented commands do not supply it. A user following this configuration advice will not enable Massive in a caller of the factory unless the variable is separately exported. The documented demo also constructs the simulator directly, so setting a Massive key cannot switch that demo to real prices.

Clarify that root `.env` consumption is part of the planned Docker launch flow, explain how to supply environment variables to current backend callers, and label the demo as always using the simulator.

## Specification follow-up

The expanded plan improves the API contract and records unresolved decisions explicitly. Before implementation, resolve the already listed trade atomicity, database initialization timing, daily change baseline, and mock response behavior; these affect correctness and shared implementation assumptions. These are acknowledged specification gaps, rather than additional findings introduced by this review.

## Validation and limits

- Inspected all changed tracked files and all new reviewer configuration files; compared the README and API descriptions with the backend dependency configuration, demo, factory, and SSE implementation.
- `git diff --check` reports trailing whitespace at `.claude/settings.json:7`.
- Attempted `uv run --no-sync pytest -q` in `backend`; execution stopped because access to the external uv cache was denied. Tests did not run, and no existing backend virtual environment was available.
- The reviewer hook was inspected statically, not activated. No live provider requests or future frontend/Docker commands were tested.

The review leaves the implementation and other documentation unchanged.
