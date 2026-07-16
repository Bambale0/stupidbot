# StupidBot

Telegram webhook-приложение BANANA на FastAPI и aiogram: генерация изображений и видео, повтор с сохранёнными референсами, Mini App, T-Bank платежи, партнерская программа, публичная лента и админ-панель.

## Production stack

- Python 3.11
- PostgreSQL
- Redis
- HTTPS-домен для Telegram webhook и Mini App
- systemd и nginx либо эквивалентный process manager/reverse proxy
- Comet и/или KIE API credentials
- T-Bank credentials для онлайн-оплаты

## Установка

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip setuptools wheel
python -m pip install -e ".[dev]"
cp .env.example .env
python -m scripts.init_db
```

`APP_ENV=production` включает обязательную проверку Telegram/callback secrets и HTTPS `PUBLIC_BASE_URL`.

## Основные переменные окружения

### Приложение

- `APP_ENV`
- `PUBLIC_BASE_URL`
- `PORT`
- `LOG_LEVEL`
- `DATABASE_URL`
- `REDIS_URL`
- `ADMIN_IDS`

### Telegram

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_SECRET_TOKEN`
- `TELEGRAM_WEBHOOK_PATH`
- `TELEGRAM_SET_WEBHOOK`
- `TELEGRAM_BOT_USERNAME`
- `MINI_APP_PATH`

### Провайдеры

- `COMET_API_KEY`
- `COMET_BASE_URL`
- `COMET_CALLBACK_SECRET`
- `KIE_API_KEY`
- `KIE_BASE_URL`
- `KIE_UPLOAD_BASE_URL`
- model-specific Comet/KIE variables из `.env.example`

### Платежи и гибридная экономика

- `TBANK_TERMINAL_KEY`
- `TBANK_PASSWORD`
- `TBANK_SUCCESS_URL`
- `TBANK_FAIL_URL`

Пользователь может одновременно использовать два способа оплаты:

- купить платную подписку на ограниченный срок; повторная покупка продлевает действующую подписку;
- отдельно покупать фиксированные пакеты фото-, видео- или универсальных кредитов.

Подписка и кредитные балансы хранятся независимо: покупка подписки не стирает кредиты, а покупка кредитов не меняет срок подписки. Стандартная подписка оплачивается разово и не имеет автопродления. Произвольная покупка пользовательского количества универсальных кредитов отключена; доступны только настроенные администратором пакеты.

## Запуск

Локально:

```bash
source .venv/bin/activate
python -m app.main
```

Production:

```bash
systemctl restart stupidbot
systemctl is-active stupidbot
journalctl -u stupidbot --since "5 minutes ago" --no-pager
```

Пример unit-файла находится в `systemd/stupidbot.service`.

## Пользовательские сценарии

- «Создать фото» открывает выбор активной модели и текущей цены.
- «Мои референсы» на экране фото-моделей показывает последние уникальные наборы Telegram `file_id`.
- Повтор собственной генерации восстанавливает фото, модель, промпт, формат и качество.
- Перед повторным запуском заново проверяются доступность модели, лимит фото, актуальная цена и баланс.
- Повтор публичной работы не копирует чужой результат и требует собственный референс.
- Две приветственные попытки применяются только к фото; видео всегда требует платную подписку либо достаточный видео/универсальный баланс.
- В разделе пополнения одновременно показываются подписка и отдельные пакеты кредитов.

## Админ-панель

Администратор может управлять пользователями, балансами, подписками, тарифами, моделями, платежами, рефералами, выводами, галереей, настройками и рассылками.

Финансовые действия выполняются идемпотентно: повторное подтверждение уже оплаченного платежа не зачисляет кредиты повторно и не продлевает подписку второй раз. Ручное подтверждение разрешено только для платежей со статусом `manual_pending`.

Рассылка запускается фоновой задачей и не удерживает Telegram webhook. Получатели читаются из PostgreSQL ограниченными пачками, заблокированные пользователи исключаются, а прогресс `sent_count`/`fail_count` сохраняется после каждой пачки. При остановке процесса незавершённая рассылка получает статус `interrupted` и не перезапускается автоматически, чтобы не отправлять сообщения повторно части аудитории.

## Проверки

### Быстрый локальный набор

```bash
bash scripts/ci.sh
```

Он выполняет:

- compileall;
- deployment safety contract;
- Telegram UX contract;
- reusable reference regression;
- gallery compatibility;
- admin smoke;
- broad current-policy regression.

### PostgreSQL/Redis gate

Workflow `.github/workflows/financial-integrity.yml` запускается для PR и push в `dev`, `main`, `master` и проверяет:

- PostgreSQL 16 и Redis 7 readiness;
- Alembic migrations;
- reusable reference flow;
- гибридную экономику подписки и кредитных пакетов;
- активацию, продление и сторно подписки;
- ручные админские подтверждения платежей и их идемпотентность;
- пакетную неблокирующую рассылку и исключение заблокированных пользователей;
- привязку реферала, aliases, self/cycle/rebind/blocked guards;
- комиссии за кредитные пакеты и подписки, повторную обработку, выводы и сторно;
- financial ledger/reversal/idempotency regressions;
- broad current-policy regression;
- transactional DB smoke;
- backup/restore drill.

Локальные команды:

```bash
python scripts/runtime_readiness.py
python scripts/reference_regression.py
python scripts/regression_financial.py
python scripts/regression_500_current.py
python scripts/staging_issue3_db_smoke.py
```

## Миграции и seed

```bash
python -m scripts.init_db
```

Команда применяет Alembic/compatibility schema и создает обязательные defaults. Она должна выполняться до запуска новой версии сервиса.

## Staging rollout

Push в `dev` запускает `.github/workflows/staging-rollout.yml`.

Gate выполняет:

1. immutable archive и checksum;
2. backup кода и PostgreSQL custom-format dump;
3. isolated restore verification;
4. candidate compile и contracts до изменения приложения;
5. миграции и regressions;
6. PostgreSQL/Redis readiness;
7. restart и systemd status;
8. локальный health;
9. публичные health, Mini App runtime и packages smoke.

Rollback остается активным до завершения public smoke. При ошибке после начала mutation восстанавливается предыдущий код; database dump сохраняется для ручного восстановления данных при необходимости.

Основной скрипт: `ops/staging_rollout.sh`.

## Manual paid smoke

Платные provider и T-Bank workflows запускаются только вручную и требуют явной confirmation phrase:

- `.github/workflows/provider-paid-smoke.yml`
- `.github/workflows/tbank-live-smoke.yml`

Не запускайте их на production-картах или без согласованного тестового бюджета.

## Архитектура

```text
app/
  main.py                  FastAPI, Telegram webhook, Mini App API
  bot.py                   aiogram dispatcher, middleware, commands
  config.py                environment settings
  db.py                    SQLAlchemy schema compatibility
  models.py                domain models and ledgers
  plugins/
    core/                   start, profile, balance, support
    generation/             image/video flows
    references/             personal repeat and saved Telegram file_id sets
    feed/                   public feed
    gallery/                legacy-compatible feed alias
    payments/               packages, subscriptions and payment UX
    partners/               referrals and withdrawals
    admin/                  operations and configuration
    finance/                financial analytics
    ux/                     production navigation contracts
  services/
    admin_hardening.py      bounded background broadcasts and recovery
    billing_catalog.py      hybrid subscription/credit catalog
    comet.py
    kie.py
    tbank.py
    task_tracker.py
    financial_*.py
scripts/
  ci.sh
  runtime_readiness.py
  reference_regression.py
  regression_admin_operations.py
  regression_billing_referrals.py
  regression_deployment_safety.py
  regression_bot_ux.py
  regression_financial.py
  regression_500_current.py
ops/
  staging_rollout.sh
  verify_postgres_restore.sh
```

## Security rules

- не коммитить `.env`, токены, private keys, dumps и реальные customer payloads;
- не выводить secrets в Actions artifacts или issue comments;
- callback/webhook signatures проверяются до финансовой mutation;
- пользователь не получает provider traceback или внутренние идентификаторы;
- отрицательные цены и балансы запрещены DB constraints;
- credit и affiliate ledgers append-only;
- повторные callbacks/finalization/refunds должны быть idempotent;
- сохранённые референсы доступны только владельцу исходной задачи.

## Release policy

`dev` — staging/release-candidate branch. `main` — выпущенная версия.

Перед merge `dev -> main` обязательны:

- отсутствие открытых P0/P1/P2 blockers;
- зеленые CI и Financial integrity;
- успешный staging rollout текущего SHA;
- backup/restore evidence;
- актуальный rollback plan;
- синхронизация `main` и `dev` после release.
