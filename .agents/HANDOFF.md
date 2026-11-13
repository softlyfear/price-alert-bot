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

## Selected Wildberries size release

Commit `e16a521` was accepted and pushed, then deployed with explicit user
approval. The running source was verified against all 63 application/migration
Python files from that commit; SQLite revision is `5d2df2075e03`.

WB links retain their selected option through preview, storage, refreshes and
alerts. Missing/unpriced selected options report out_of_stock without another
size's price. Legacy rows retain size 0 and minimum-price behavior; users must
add a new size-bearing link to select a specific option.

Verification: 3325 tests passed, strict typing/lint/import checks passed, and
changed application code plus the migration reached 100% line/branch coverage.
Independent review and architecture acceptance completed. One Ozon browser
navigation test failed once and passed targeted/full repeats; its cause remains
unknown, and Ozon short-link investigation remains separate.

Deployment preserved every pre-existing user/product/alert field before app
startup (2 users, 4 products, 4 alerts), verified a frozen project-local backup,
and retained a checked rollback image. The original Redis container remained
healthy. Runtime checks: HTTP 200, one app instance, live polling/scheduler tasks,
4 products checked with 0 failures, source hashes and synthetic size-selection
probe passed. No Telegram test messages were sent. Next user check: submit the
WB link with the intended size again and verify the displayed size and price.


## Security dependency maintenance — PAB-127 (2026-11-13)

Targeted dependencies and runtime tooling/packages were updated without changing
application behavior or data paths. aiogram 3.29.1 is required for aiohttp 3.14.4;
existing test fixtures/assertions were adapted to its Bot API types. Mako 1.4.2
also covers the subsequent Windows traversal advisory. Image uv is 0.13.5;
system pip is 26.3, with patched vendor bundles; APT upgrades are limited to
apt/libapt and zlib1g.

Verification: 3325 tests, 100% line/branch application coverage, strict mypy,
ruff and seven import contracts passed. Independent Ship, three positive
controls and three killed mutants; architect accepted. Final image build and
isolated non-root filesystem/OpenAPI smoke passed. Five targeted Python OSV
queries returned no findings; no full image or repository clean scan is claimed.
Unrelated WORKFLOW edits are preserved outside this task. No deployment or live
data/credential operation was performed. Next external condition: authorized
replacement of the deployed image and runtime verification, if deployment is requested.
