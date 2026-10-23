# price-alert-bot

Telegram-бот, который следит за ценами на **Wildberries** и **Ozon** и сообщает, когда товар подешевел или подорожал до нужной вам цены.

## Возможности

- Добавление товара по ссылке, короткой ссылке Ozon «Поделиться» или артикулу.
- Порог в рублях. Направление выбирается само: ниже текущей цены — «сообщить, когда дешевле», выше — «когда дороже».
- Проверка цен по расписанию (по умолчанию раз в 15 минут), повторное уведомление не чаще раза в сутки.
- До 50 товаров на пользователя, постраничный список «Мои товары».
- Цена без персональных скидок маркетплейса (кошелёк, карта, баллы): на сайте она может быть ниже.

| Команда | Что делает |
|---|---|
| `/start` | приветствие и список команд |
| `/help` | подробнее о боте |
| `/add` | добавить товар в отслеживание |
| `/list` | мои товары и пороги |
| `/cancel` | отменить текущее действие |

## Стек

Python 3.14 · aiogram 3 · FastAPI · PostgreSQL · SQLAlchemy 2 · Alembic · Redis · Playwright (Chromium) · curl_cffi · Docker

## Установка на сервер

Нужны Docker с плагином `compose`, `git` и `make`.

```bash
git clone https://github.com/softlyfear/price-alert-bot.git
cd price-alert-bot
cp .env.example .env
nano .env            # заполнить переменные, см. ниже
make docker-up       # сборка образа, миграции, запуск
```

Минимум, который нужно заполнить в `.env`:

| Переменная | Что это |
|---|---|
| `TG__BOT_TOKEN` | токен бота от [@BotFather](https://t.me/BotFather) |
| `DB__USER`, `DB__PASSWORD`, `DB__NAME` | учётные данные PostgreSQL (создаются при первом запуске) |
| `DB__ECHO` | `False` на сервере |
| `OZON__PROXY_URL` | необязательно: SOCKS5-прокси для Ozon, `socks5h://<логин>:<пароль>@<хост>:<порт>` |

Остальные переменные можно оставить как в `.env.example`. Хосты и порты PostgreSQL и Redis стек подставляет сам. Секреты лучше составлять из символов `A-Z a-z 0-9 _ -`.

> Один токен — один запущенный бот. Если бот уже работает на другой машине, сначала остановите его там, иначе Telegram вернёт `TelegramConflictError`.

## Управление

| Команда | Что делает |
|---|---|
| `make docker-up` | собрать, применить миграции и запустить (и для обновления после `git pull`) |
| `make docker-logs` | журнал |
| `make docker-restart` | перезапуск |
| `make docker-down` | остановить стек (данные в томах сохраняются) |

Все сервисы стека под профилем compose `price-alert-bot`: обычные `docker compose up/down/restart` без `--profile price-alert-bot` его не трогают.

## Разработка

Нужны Python 3.14, [`uv`](https://docs.astral.sh/uv/) и запущенные PostgreSQL и Redis, адреса которых указаны в `.env`.

```bash
make install           # зависимости
make install-browser   # Chromium для Playwright
make hooks             # git-хуки
make run               # миграции и сервер с автоперезагрузкой
```

Проверки перед коммитом:

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy app tests
uv run lint-imports
uv run pytest --cov=app
```

Остальные цели — `make help`.

## Лицензия

MIT — см. [LICENSE](LICENSE).
