# Shared handoff

This file carries the portable project summary for Codex. Detailed private
architecture/ticket files under `docs/` are intentionally ignored and may be
absent in another checkout. They are not the only place to save decisions or
open questions needed by the next client.

## Project instruction maintenance — 2026-11-13

Native role prompts are English and retain model/effort metadata and layer ownership. Conflicting ticket/architecture requirements return to the orchestrator. The diagnosing-bugs skill has valid YAML, and shared records are updated only for change-authorizing requests. Independent static review passed after correcting test-ownership wording; tooling tests, pre-commit, skill validation and prompt-input discovery passed. The unchanged pre-push hook also passed for the preserved application snapshot. A prior shared-environment mismatch was reproduced on original HEAD and checked successfully with an isolated locked environment. No diagnostic model evaluation, product implementation change or deployment was performed by this instruction task.

## Agent synchronization

Repository synchronization uses `scripts/sync_upstream.py` and `.agents/WORKFLOW.md`.
The selected server checkout has the user-approved local opt-in for safely
accepting rewritten upstream history. Git sync does not establish which
container image or database migration is running; runtime state is checked
separately when a task needs it.

## Product work

The current accepted product invariants are in `AGENTS.md`; implementation and
verification history are in Git. Uncommitted work in another checkout is not
published by this synchronization task. Add the verified outcome and remaining
questions here when that work is completed, without copying private deployment
details or the full local ticket ledger.
