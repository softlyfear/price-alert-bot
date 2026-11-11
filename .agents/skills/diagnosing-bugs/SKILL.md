---
name: diagnosing-bugs
description: "Diagnose a price-alert-bot failure or performance regression from a reproducible check before testing hypotheses. Use for failing or flaky tests, tracebacks, missing or duplicate notifications, incorrect bot responses, IntegrityError, shutdown hangs, or WB/Ozon anti-bot failures."
---

# Diagnose a bug

Adapted from `mattpocock/skills` (`diagnosing-bugs`). Explain any necessary departure from this sequence.

## 1. A check that fails for the reported symptom

Establish a reproducible check before hypothesis-driven exploration. Prefer:

1. A failing boundary test: `uv run pytest tests/... -k <scenario> -x`.
2. A saved response in `tests/fixtures/<marketplace>/` through a mocked transport: WB uses `httpx.MockTransport`, Ozon uses `FakeOzonSession`. Sanitize a newly saved fixture and quote only diagnostically relevant lines.
3. A one-off mocked service harness using `uv run python -c ...`.
4. For intermittent failures, controlled repeated inputs that make the failure reproducible.
5. For a commit regression, `git bisect run` with the check, only within an isolated copy that preserves the shared tree.

Run the check at least once and retain its command/result. It must test the actual symptom, fix time/randomness, run quickly and require no human input. Live WB/Ozon/Telegram requests require explicit approval and never belong in automated tests.

If no check can be built, report attempts and the missing environment/artifact; pause dependent diagnosis while continuing independent authorized work.

## 2. Reproduce and narrow

Confirm it is the reported failure rather than a neighboring one. Remove inputs, steps and configuration until every retained element affects reproduction.

## 3. Hypotheses

Before testing a candidate, present 3–5 ranked falsifiable hypotheses: what change would remove the failure and what would strengthen it. Do not block on a reply to this hypothesis list.

## 4. Instrumentation

One probe tests one prediction/variable. Mark temporary logs with `[DEBUG-<4 characters>]` for targeted cleanup. For performance, measure timing or `EXPLAIN ANALYZE` before changing behavior.

## 5. Fix and regression

Write the regression before the fix when an observable test boundary reproduces the scenario. A missing boundary is a finding for the architect. The layer owner implements the fix under the root pipeline; `tests-agent` owns the assigned tests.

## 6. Completion

The original check passes; the regression passes or its missing seam is recorded; `grep -rn "\[DEBUG-" app tests` has no temporary matches. Name the confirmed hypothesis so the next investigation can reuse the result.
