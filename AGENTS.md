# price-alert-bot

Telegram price tracker for Wildberries and Ozon. Users add products and choose
notification thresholds. User-facing Telegram text is Russian; preserve exact
workflow verdict strings.

OpenCode roles live in `.opencode/agents/`, project settings in `opencode.jsonc`,
and skills in `.agents/skills/`. Detailed local architecture and tickets live in
`docs/PROJECT.md`, `docs/TASKS.md`, and `docs/TASKS-archive.md`. Portable
continuation summaries live in `.agents/HANDOFF.md`; it is not a second ticket
ledger. Do not add a second project agent runtime beside OpenCode.
Keep ticket history, investigation dates, and long rationale in project docs.

## Stack

| Area | Technology / constraint |
|---|---|
| Python | 3.14, `.python-version` |
| Packages | uv; commit `uv.lock` |
| Bot | aiogram 3, long polling |
| FSM | RedisStorage, persistent across restarts |
| Web | FastAPI + uvicorn, lifespan, `/health` |
| Database | SQLite, SQLAlchemy 2 async, aiosqlite; one bot instance |
| Migrations | Alembic |
| HTTP | Shared process-wide httpx AsyncClient |
| Browser | Playwright Chromium for Wildberries tokens and Ozon cookies |
| Ozon HTTP | curl_cffi Chrome TLS fingerprint, only in `ozon_client` |
| Logging | loguru, `logger.bind()` context |
| Scheduling | asyncio loop; `.env` interval, default 15 minutes |
| Tests | pytest/pytest-asyncio, mocked units and file-SQLite integration |
| Static checks | mypy strict, ruff |

The redis dependency exists for FSM storage.

## Approved product behavior

- Add products through an FSM: link/article → product/current price → threshold
  → confirmation. Infer direction: lower threshold is `below`, higher is `above`;
  reject equality and ask for another value. Higher-price alerts are supported.
- Alerts stay active after triggering. `.env` cooldown starts at `triggered_at`.
- Default limit is 50 products per user, configurable through `.env`.
- Ozon uses public `entrypoint-api` and browser anti-bot access. Failed access is
  an expected, explicit state, not a product-not-found result (`PROJECT.md` §2.14).
- Money is integer kopecks; the bot layer converts ruble input. Prices exclude
  personal wallet/points discounts, stated wherever the bot displays a price.
- Ozon prefers `cardPrice`, falling back to regular price. The add dialog uses
  «Текущая цена по карте Ozon».
- All timestamps are timezone-aware UTC.
- Every Product/Alert operation is scoped to the caller's `user_id`; callback
  IDs are untrusted input. Return domain messages rather than stack traces.

## Ticket pipeline

One ticket is a completed vertical user/operator outcome, not a technical
layer. It has one review, acceptance, and commit. Split chains exceeding about
four implementers into separate coherent slices. Layer-only tickets are for
outcomes wholly within that layer, such as build, migration, or review cleanup.

`python-architect` defines the ticket and AC → required layer owners implement
in dependency order → `tests-agent` handles necessary test changes last → final
gate owner runs the full gate → independent `code-reviewer` reviews/mutates →
architect accepts with `✅ ПРИНЯТО` or returns `❌ НА ДОРАБОТКУ`.
Review verdicts are `Ship` / `No-Ship`. A rejection returns findings to the
affected owners; only the architect updates `Попытка` in `docs/TASKS.md`.

- For configuration/instruction work the orchestrator owns `AGENTS.md` and
  OpenCode project config (`opencode.jsonc`, `.opencode/`). `devops-agent` owns
  packaging, CI, and operational scripts. Do not involve unaffected product
  layers for a simple configuration edit.
- Role definitions are `.opencode/agents/<role>.md`. `python-architect` and
  `code-reviewer` use `openai/gpt-6.1-sol#high`. The nine layer roles use
  `openai/gpt-6.1-terra#high`. These are the installed OpenCode catalog IDs for
  the previous Sol/Terra high split. Do not invent aliases. When role loading
  is unavailable, pass its instructions, model, and variant explicitly; use
  fresh context if the tool requires it.
- Delegation includes ticket ID, AC, allowed files, tree state, and prior results.
  Parallel work must not overlap writes, dependency synchronization, or Git.
  No more than three children at once; the environment's actual limit prevails.
- A layer owner requests foreign-layer edits through
  `Требуется правка в чужом слое` rather than editing those files.
- Only the orchestrator commits or switches branches. Start the next ticket
  after review and acceptance. Role file boundaries are behavioral instructions,
  not separate filesystem sandboxes.
- Shell safety and post-edit Python lint live in the `project-guard` plugin.
  OpenCode's ruff formatter formats Python after `write`, `edit`, and `patch`.
  Changed plugin files apply on the next config reload.

### Owners

| Role | Files |
|---|---|
| `db-models-agent` | `app/models/**`, `alembic/**` |
| `schemas-agent` | `app/schemas/**` |
| `repository-agent` | `app/repositories/**` |
| `integrations-agent` | `app/services/{base_client,wb_client,ozon_client,client_factory,wb_access,wb_browser,browser_launch,ozon_access,ozon_browser,socks_relay}.py` |
| `service-agent` | Other `app/services/**`, `app/domain/**` |
| `bot-agent` | `app/bot/**`, `app/handlers/**` |
| `core-agent` | `app/core/**`, `app/main.py`, `app/scheduler.py`, `.env.example` |
| `tests-agent` | `tests/**` |
| `devops-agent` | Dockerfile, docker-compose.yml, Makefile, pyproject.toml, pre-commit config, CI |

### Checks and review evidence

Each implementer runs targeted checks. The final owner runs the gate once after
the slice is complete and reports commands, exit codes, coverage, limitations,
and the inspected diff including untracked files. Reviewer/architect reuse
current evidence and can reproduce a specific finding. Missing or stale
evidence returns to the gate owner. Relevant implementation/test/dependency/
check-environment changes require updated evidence and a fresh full code gate.

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy app tests
uv run lint-imports
uv run pytest --cov=app --cov-report=term-missing
```

Require zero mypy errors, zero broken import-layer contracts, clean ruff, and
100% coverage of ticket code. Configuration/instruction-only edits are checked
for syntax, meaning, and loading; do not invent text coverage or code mutations.
CI and Git-hook checks remain in force. `mypy` is the type authority; pyright LSP
is navigation, and a pyright-only diagnostic is not automatically a defect.
For post-edit guard changes, also run
`node --test scripts/tests/test_project_guard.mjs`; CI runs these regressions.

Review mutations use a copy outside the working tree. Any mutation left in the
working tree blocks acceptance. Repeat reviews are incremental: attempt delta,
prior findings, current gate report, and mutations of new/changed tests only.
Read ticket conventions and the current card rather than all archived history.
Reports are compact: about 50 lines per implementer, 80 per reviewer, concise
architect acceptance without repeating the review. Out-of-ticket edits block
acceptance.

## Library documentation and skills

Context7 is the project MCP server `context7` in `opencode.jsonc`. Use available
session tools.
For uncertain API behavior or unexpected library behavior, query the version
from `uv.lock`. Known IDs can go directly to `query-docs`; at most three calls
per question. Fallback is matching official documentation.

| Library | Context7 ID |
|---|---|
| aiogram | `/aiogram/aiogram` |
| pydantic | `/pydantic/pydantic` |
| SQLAlchemy | `/websites/sqlalchemy_en_20` |
| httpx | `/encode/httpx` |
| loguru | `/delgan/loguru` |
| alembic | `/websites/alembic_sqlalchemy` |

Questions contain only library/API information. AC depending on an API requires
execution evidence as well as documentation. `diagnosing-bugs` lives under
`.agents/skills/`. Use the session's `skill-creator` for skill authoring rather
than copying its general authoring rules here.

Probe cleanup must validate the exact target. For variable paths use nonempty
guards such as `rm -rf "${D:?}/${m:?}"`; a trusted project guard is only an
additional check.

## Commands and Git

Author and committer must be the user from `git config user.name` and `git config user.email`.

`make install` synchronizes uv; `make fmt` formats/fixes ruff; `make type`,
`make test`, `make test-cov`, and `make db-upgrade` run their named checks or
Alembic upgrade. Work directly in `main`, without extra branches. One accepted
vertical ticket per commit; dependent tickets that cannot pass separately share
one commit. Keep commit bodies to one or two lines when sufficient.

## Project records

Only `python-architect` edits the following Git-excluded protocol documents and
does not edit implementation files:

- `docs/PROJECT.md`: approved architecture.
- `docs/TASKS.md`: conventions, checklists, open decisions, active ticket cards;
  targeted edits, approximately 150 KB budget.
- `docs/TASKS-archive.md`: closed cards, decisions, and agreement history;
  append-only.

## Repository synchronization

For safe upstream synchronization use [.agents/WORKFLOW.md](.agents/WORKFLOW.md).
Its local Git opt-in records the specifically approved server-sync exception.
