# CampusPilot-AI — working rules for AI coding assistants

Scope: Assignment 1 only. Follow these rules in every task. Detailed history lives in `PROJECT_STATE.md`; user-facing docs live in `README.md`.

## Keep documentation current (in the same task as the code change)

- Update `PROJECT_STATE.md` whenever an approved change affects:
  - architecture, modules or file responsibilities;
  - features added, changed or removed;
  - API endpoints, schemas or integrations;
  - memory, privacy, security or error-handling behaviour;
  - dependencies, configuration or environment requirements;
  - known bugs, limitations, pending work or design decisions;
  - test results (see below).
- Update `README.md` whenever a change affects setup instructions, dependencies, features, the architecture summary, API usage, supported behaviour, known limitations or other user-facing documentation. Do not update it for internal refactors that change none of that.
- If a document cannot be updated confidently, say what information is missing and ask. Do not guess.

## Keep documentation truthful

- Never claim a test passed unless it was actually run and passed.
- Record test results with the exact command, the counts (passed / failed / skipped / xfailed), whether the tests were actually re-run, and the date when useful.
- Label results clearly as freshly verified (with date) or previously recorded. Keep historical results as history.
- Keep these categories separate: offline tests, skipped tests (the opt-in live Gemini test), expected failures (the documented xfails), live API evaluations, and quota-blocked evaluations.
- Do not describe a quota-blocked evaluation (HTTP 429) as an application failure unless evidence supports that conclusion.
- Never invent implementation details or test results.

## After an approved code change

1. Update the relevant documentation.
2. Run the appropriate tests (offline only).
3. Check that documentation and code agree.
4. Run `git diff --check`. While the project is untracked, that check finds nothing, so also compare each edited file against a backup copy with `git diff --no-index --check`.
5. Report every changed file and the tests actually run.

For documentation-only changes, do not re-run the full test suite unless there is a specific reason.

## Boundaries

- Preserve existing functionality unless explicitly approved otherwise. Do not modify unrelated files.
- Never read, print, copy or expose secrets from `.env` or any credential file.
- Never run live model calls or live evaluations, or incur API cost, without explicit approval. Note that `scripts/run_evaluation.py` with no flag is LIVE whenever an API key is configured: always pass `--offline`. Run tests with `RUN_LIVE_TESTS` unset.
- Never commit, stage or push without explicit approval.
- Do not start Assignment 2. The current scope stays Assignment 1 until the project owner explicitly approves the transition; Assignment 2 work may begin only after that approval, and this file will then be updated.
