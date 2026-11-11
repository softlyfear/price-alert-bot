# Project map

The application runs Telegram long polling and scheduled price checks in one
process. Start at [app/main.py](app/main.py): its FastAPI lifespan wires resources
and background tasks; `/health` reports application health.

| Area | Responsibility and entry points |
| --- | --- |
| Telegram | [app/bot/](app/bot/) contains texts, keyboards, FSM states and middleware; [app/handlers/](app/handlers/) handles user actions. Product entry starts in `add_product.py`. |
| Domain | [app/domain/](app/domain/) parses product references, converts money and chooses threshold directions. |
| Contracts | [app/schemas/](app/schemas/) validates marketplace responses and defines shared fetch results. |
| Use cases | [tracking.py](app/services/tracking.py) adds and manages tracked products; [price_service.py](app/services/price_service.py) applies prices and runs manual refreshes; [notification.py](app/services/notification.py) sends alerts. |
| Marketplace access | [client_factory.py](app/services/client_factory.py) selects WB or Ozon clients. Adjacent access/browser modules obtain anti-bot access; Ozon uses `curl_cffi`. |
| Persistence | [app/repositories/](app/repositories/) queries [app/models/](app/models/); [alembic/](alembic/) versions the SQLite schema. |
| Runtime | [app/core/](app/core/) owns settings, database sessions and logging; [app/scheduler.py](app/scheduler.py) batches background work. |
| Verification | [tests/](tests/) contains mocked unit tests, captured fixtures and file-SQLite integration tests; [pyproject.toml](pyproject.toml) defines dependencies and quality checks. |
| Agent continuity | [.agents/WORKFLOW.md](.agents/WORKFLOW.md) documents safe upstream synchronization; [scripts/sync_upstream.py](scripts/sync_upstream.py) synchronizes the configured upstream with local-work protection. |

Product flow: Telegram input → domain reference → tracking preview → marketplace
client → FSM confirmation → user-scoped product and alert records. A WB reference
with `size` retains its option ID through storage, refreshes and product links.

Price flow: scheduler or manual refresh → fetch once per article/batch → apply
the shared snapshot to each tracked product → evaluate thresholds → notification.
Selected WB sizes use their own price; an unavailable selected size never falls
back to another. Products without a selected size retain the minimum-price rule.

Runtime dependencies and startup order are in [docker-compose.yml](docker-compose.yml).
[.env.example](.env.example) documents settings; persistent bind mounts are under
`data/`. [Makefile](Makefile) provides local checks, storage and startup commands.
