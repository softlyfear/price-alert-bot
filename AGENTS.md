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

OpenCode starts new sessions with the project `orchestrator` primary agent.

One ticket is a completed vertical user/operator outcome, not a technical
layer. It has one independent review and one commit. Split chains exceeding about
four implementers into separate coherent slices. Layer-only tickets are for
outcomes wholly within that layer, such as build, migration, or review cleanup.

`python-architect` defines the contract, ticket, and AC → assign one implementer
when the scope fits one role, otherwise use only the necessary layer owners →
final implementer runs the applicable gate → independent `code-reviewer`
reviews the complete change and returns `Ship` or `No-Ship`.

Trivial single-layer corrections with an unambiguous outcome may skip the
architect: the orchestrator records the ticket ID, AC, owner and allowed paths
in `docs/TASKS.md`, the owner implements it, and the same gate and independent
review still apply. Architecture changes, cross-layer work, and ambiguous
requirements always use the architect.

The orchestrator checks that the verdict and gate evidence apply to the exact
candidate. On `Ship`, record `✅ ПРИНЯТО`, close the ticket, and commit. On
`No-Ship`, record `❌ НА ДОРАБОТКУ`, increment `Попытка` once, and route the
findings back to the affected owners. Pending context or checks are not a
completed review attempt. This is administrative, not a second technical
review. Consult the architect again only for a changed or ambiguous contract;
if the contract changes, the reviewer checks the affected criteria again.

- For configuration/instruction work the orchestrator owns `AGENTS.md` and
  OpenCode project config (`opencode.jsonc`, `.opencode/`). `devops-agent` owns
  packaging, CI, and operational scripts. Do not involve unaffected product
  layers for a simple configuration edit.
- Role definitions are `.opencode/agents/<role>.md` and pin no model: a subagent
  with no `model` field inherits the model and reasoning variant of the running
  session, so one provider's subscription quota or outage never blocks the role
  chain. Choose the session model and its reasoning level instead of editing
  role files; do not add `model:` back without an explicit decision. Per-role
  reasoning levels are not available this way: OpenCode's `variant` applies only
  when the agent has its own configured model, and not every model offers every
  level. When role loading is unavailable, pass the role instructions explicitly
  in a fresh context; the model stays the session's.
- Role files must stay vendor-neutral: no provider names, model IDs or reasoning
  levels in the instructions or the delegation prompts.
- Delegation includes ticket ID, AC, allowed files, tree state, and prior results.
  Parallel work must not overlap writes, dependency synchronization, or Git.
  No more than three children at once; the environment's actual limit prevails.
- A layer owner requests foreign-layer edits through
  `Требуется правка в чужом слое` rather than editing those files.
- Only the orchestrator commits or switches branches. Start the next ticket
  after review and acceptance. Role file boundaries are behavioral instructions;
  native permissions enforce the read, edit, and shell restrictions only where
  an agent declares them.
- The architect can edit only design and ticket records. The reviewer reads
  project files and parent-provided evidence without shell or edit access.
  The reviewer specifies and assesses independent mutation probes; the
  orchestrator runs them on a disposable copy and returns the actual results.
- OpenCode's ruff formatter formats Python after `write`, `edit`, and `patch`.

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

Assign only owners whose files the ticket changes; do not convene unaffected
roles.

### Checks and review evidence

Each implementer runs targeted checks. The final owner runs the gate once after
the slice is complete and reports commands, exit codes, coverage, limitations,
and the inspected diff including untracked files. The reviewer reuses
current evidence and can specify a targeted reproduction for a specific
finding. Missing or stale evidence returns to the gate owner. Relevant
implementation/test/dependency/check-environment changes require updated
evidence and a fresh full code gate.

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
Reports are compact: about 50 lines per implementer and 80 per reviewer.
Out-of-ticket edits block `Ship`.

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

The architect owns design and ticket issuance, except for the trivial path
above; never implementation or closure. The orchestrator owns attempt counters
and closure. The following protocol documents are Git-excluded:

- `docs/PROJECT.md`: approved architecture.
- `docs/TASKS.md`: project rules and open tickets only — conventions, the AC
  checklist, the ledger rules and card template, plus issued open tickets;
  targeted edits, approximately 40 KB budget.
- `docs/TASKS-archive.md`: closed cards, acceptance protocols, decisions and
  agreement history; bounded by the retention rule below.

### Ticket ledger retention

The ledger must not accumulate closed work. Acceptance moves the card and its
protocol into the archive in one insert and deletes that ticket's checklist row
and card from `docs/TASKS.md`, so a closed ticket leaves no trace in the active
file. The archive keeps the cards and protocols of the 30 most recently
accepted tickets; when an acceptance pushes the count past that bound,
the orchestrator deletes the oldest ticket entries, including older
transition snapshots. Normative non-ticket blocks (agreement texts with their
history, the archived `PROJECT.md` material) are not tickets and stay.

IDs are never reused: numbering continues from the highest issued ID (the next
ticket after `PAB-139` is `PAB-140`) even though old cards are gone, and commits
address a ticket by `(PAB-NNN)` in the subject. Because these documents are
Git-excluded, a deleted record is unrecoverable: decisions that must outlive a
ticket belong in `docs/PROJECT.md` and in the commit message, not only in the
archive.

## Repository synchronization

For safe upstream synchronization use [.agents/WORKFLOW.md](.agents/WORKFLOW.md).
Its local Git opt-in records the specifically approved server-sync exception.
