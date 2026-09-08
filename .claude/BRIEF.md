# Бриф для python-architect — первый вызов

Передать целиком в task instruction при первом вызове `python-architect`. Требования согласованы с заказчиком, Шаг 1 повторно не проводить.

Задание на первый вызов: Шаг 1 (вывести требования списком) → Шаг 2 (архитектура) → Шаг 3 (создать `.claude/PROJECT.md` и `.claude/TASKS.md`) → Шаг 4 (выдать первый тикет, либо до трёх связанных зависимостью).

## Цель фичи

Пользователь добавляет товар Wildberries или Ozon в отслеживание и задаёт цену, при достижении которой приходит уведомление в Telegram. Это главная незакрытая фича. Проект начат давно и брошен незаконченным; задача — довести до рабочего состояния.

## Согласованные решения (не пересматривать)

### Рантайм и стек
1. Python 3.14, `uv`, PostgreSQL, SQLAlchemy 2.0 async + asyncpg, alembic, httpx, loguru, pydantic / pydantic-settings.
2. Бот — `aiogram` 3, **long polling**.
3. `FastAPI` + `uvicorn` **остаются**: один процесс, в `lifespan` поднимаются polling бота и фоновый планировщик, плюс `/health`. Осознанная точка роста под будущий web API и админку — заказчик хочет их позже.
4. `celery` **удаляется** из зависимостей — не используется нигде.
5. `redis` **остаётся** — только ради `RedisStorage` как FSM-хранилища aiogram (состояния переживают рестарт).
6. Планировщик — собственный asyncio-цикл (не APScheduler, не Celery beat). Интервал из `.env`, по умолчанию 15 минут.
7. Тесты — `pytest` + `pytest-asyncio`: unit на моках, интеграционные на **реальном PostgreSQL** (демон Docker доступен, `testcontainers` использовать можно).
8. Quality gate — `mypy` со `strict = true` (уже в `pyproject.toml`) и `ruff`. 100% покрытия на коде тикета.

### Продукт
9. Добавление товара — **FSM-диалог шаг за шагом**: ссылка или артикул → бот показывает найденный товар и текущую цену → ввод порога → подтверждение. Нужна отмена на любом шаге.
10. Направление порога в UI — **только `below`** («сообщить, когда дешевле»). `AlertDirection.above` остаётся в модели на будущее, пользователю не показывается.
11. После срабатывания алерт **остаётся активным**; повторное уведомление ограничено **cooldown из `.env`**, отсчёт от `triggered_at`.
12. Лимит — **50 товаров** на пользователя, значение из `.env`. В `ProductRepository` уже есть неиспользуемый `count_active_by_user` — он под это.
13. Маркетплейсы: Wildberries (клиент работает) + **Ozon через публичный `entrypoint-api`** (`api.ozon.ru/entrypoint-api.bx/page/json/v2?url=/product/<id>`), без ключей. Заказчик предупреждён, что Ozon защищён антиботом и гарантий нет. Требование: блокировка антиботом распознаётся как **отдельная категория отказа** и честно логируется, а не маскируется под «товар не найден». Раскомментировать `Marketplace.ozon` в `app/models/enums.py`.
14. Деньги везде — целые копейки. Пользователь вводит рубли, конвертация на границе слоя бота.
15. Время везде — timezone-aware UTC.
16. Язык: docstrings и комментарии английские (как в существующем коде), все тексты для пользователя Telegram — русские.

### Процесс
17. Коммит делает оркестратор после вердикта `✅ ПРИНЯТО`, один тикет — один коммит. Исполнители не коммитят.
18. Правки строго минимальные и точечные. Заказчик отдельно подчеркнул: не переписывать пол-проекта ради стиля.

## Фактическое состояние кода

### Реализовано и работает
- `app/models/` — `Base` + `TimestampMixin`, `User`, `Product`, `Alert`, `enums.py`. Есть `UniqueConstraint`, `CheckConstraint`, каскады. Одна миграция `alembic/versions/c55fadc558a3_recreate_database.py`.
- `app/repositories/` — `BaseRepository` (ABC), `SQLAlchemyRepository` (generic, валидация полей create/patch, пагинация), `UserRepository` (с `get_or_create_by_tg_id` через PG `ON CONFLICT`), `ProductRepository`, `AlertRepository`.
- `app/services/base_client.py`, `wb_client.py` (рабочий клиент WB), `client_factory.py`.
- `app/services/price_service.py` — `PriceService.check_product`, логика пересечения порога.
- `app/services/notification.py` — `NotificationService.send_alert`.
- `app/schemas/marketplace.py` — `MarketplaceProductData`.
- `app/domain/exceptions.py` — исключения репозиторного слоя.
- `app/core/config.py`, `database.py`, `deps.py`.

### Не сделано / сломано
- `app/handlers/__init__.py` — **пустой файл**. Хендлеров нет вообще, главная фича не реализована.
- `app/bot/middlewares/db.py` — **пустой файл**. DI сессии в хендлеры отсутствует.
- `app/bot/setup.py` — только создаёт `Bot` и `Dispatcher` на уровне модуля; FSM-хранилище не настроено, роутеры не подключены.
- `app/scheduler.py` — заглушка `async def price_price_check_loop(session)` с `while True: pass` (в имени опечатка `price_price`).
- `app/main.py` — голый `app = FastAPI()`, ничего не подключено, `lifespan` отсутствует.
- `tests/` — директории нет вообще.
- `Dockerfile` — placeholder `FROM ubuntu:latest` + `ENTRYPOINT ["top", "-b"]`.
- `docker-compose.yml` — **отсутствует**, хотя `Makefile` ссылается на него в целях `docker-up` / `docker-down` / `docker-logs`.
- `README.md` — пустой (0 байт).

## Baseline-дефекты (учесть при планировании)

1. **`app/__init__.py` отсутствует.** `uv run mypy app` падает уже сейчас, до любых изменений:
   `app/core/__init__.py: error: Source file found twice under different module names: "core" and "app.core"`
   Блокирует quality gate для всех тикетов → чинить первым.
2. **`.env` отсутствует, а `get_settings()` вызывается на уровне модуля** в `app/core/database.py` (`settings = get_settings()`, затем сразу `create_async_engine`) и в `app/bot/setup.py`. Импорт падает с ошибкой валидации pydantic, движок БД создаётся в момент импорта. Прямо ломает тестируемость: `pytest` не сможет импортировать код без боевого окружения. Требует архитектурного решения (ленивая инициализация / фабрика / DI).
3. `PriceService.check_product` и `NotificationService.send_alert` используют `logger.exception("...", alert_id=..., product_id=...)`. У `loguru` произвольные `kwargs` **не** становятся полями структурного лога — контекст задаётся через `logger.bind(...)`. В `price_service.py` в одном месте `bind` применён правильно, в остальных нет. Диагностический контекст теряется.
4. `NotificationService.send_alert` проставляет `alert.triggered_at`, но алерт нигде не деактивируется и cooldown не реализован — на каждом проходе планировщика пользователь получит повторное уведомление. С учётом решения №11 нужна проверка cooldown перед отправкой.
5. `AlertRepository._required_non_nullable_fields` включает `is_active`, которого нет в `_create_fields` → создание алерта гарантированно бросает `RequiredFieldCannotBeNoneError`. **Создание алерта сейчас невозможно в принципе.**
6. `wb_client.py`: `price = price_info.get("product")`, затем `int(price)` без проверки на `None` → `TypeError` в проде при изменении формата ответа. Разбор идёт цепочкой `.get()` вместо валидации схемой.
7. `Marketplace` содержит только `wb`, `ozon` закомментирован.
8. `Makefile`, цель `type` — глушит ошибку mypy через `|| echo "⚠ Type issues found (non-critical)"`, gate не срабатывает.
9. В `Product` нет индекса под выборку планировщика `get_all_with_active_alerts` — оценить необходимость.

Baseline на момент старта: `uv run ruff check .` → `All checks passed!`; `uv run ruff format --check .` → `35 files already formatted`; `uv run mypy app` → падает по пункту 1.

## Окружение (проверено)

- `uv 0.11.31`, Python 3.14.4, ruff 0.15.7, mypy 1.19.1, pytest 9.0.2 — установлено и работает.
- Docker 29.1.3, демон доступен → `testcontainers` и `docker compose` использовать можно.
- `psql` не установлен, PostgreSQL локально не поднят. `.env` отсутствует.

## Агенты-исполнители

| Агент | Файлы |
|---|---|
| `db-models-agent` | `app/models/**`, `alembic/**` |
| `schemas-agent` | `app/schemas/**` |
| `repository-agent` | `app/repositories/**` |
| `integrations-agent` | `app/services/{base_client,wb_client,ozon_client,client_factory}.py` |
| `service-agent` | `app/services/{price_service,notification}.py`, `app/domain/**` |
| `bot-agent` | `app/bot/**`, `app/handlers/**` |
| `core-agent` | `app/core/**`, `app/main.py`, `app/scheduler.py`, `.env.example` |
| `tests-agent` | `tests/**` |
| `devops-agent` | `Dockerfile`, `docker-compose.yml`, `Makefile`, `pyproject.toml`, `.pre-commit-config.yaml`, CI |

Границы жёсткие: агент не трогает чужие файлы, а возвращает требование к соседнему слою. Тикет должен ложиться в границы одного агента; связанные правки в разных слоях оформляются отдельными тикетами с `Depends on`.
