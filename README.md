# StupidBot

Telegram webhook bot на FastAPI + aiogram для BANANA: генерация изображений/видео, Mini App, платежи, партнерка и админ-панель.

## Требования

- Python 3.10+
- PostgreSQL
- Redis
- HTTPS-домен для Telegram webhook и Mini App
- systemd/nginx для production-деплоя
- API-ключи провайдеров генерации: Comet/KIE
- T-Bank credentials, если включена онлайн-оплата

## Установка

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -U pip setuptools wheel
python3 -m pip install -e ".[dev]"
cp .env.example .env
```

Создать PostgreSQL-базу и поднять Redis. Затем заполнить `.env` и применить миграции/seed defaults.

## Переменные окружения

Основные:

- `TELEGRAM_BOT_TOKEN` — токен Telegram-бота
- `ADMIN_IDS` — Telegram ID администраторов через запятую
- `DATABASE_URL` — PostgreSQL URL
- `REDIS_URL` — Redis URL для FSM/state
- `PUBLIC_BASE_URL` — публичный HTTPS URL
- `PORT` — порт FastAPI/uvicorn
- `LOG_LEVEL` — уровень логов, например `INFO`

Webhook/Mini App:

- `TELEGRAM_SET_WEBHOOK`
- `TELEGRAM_WEBHOOK_PATH`
- `TELEGRAM_WEBHOOK_URL`
- `TELEGRAM_SECRET_TOKEN`
- `MINI_APP_PATH`
- `TELEGRAM_BOT_USERNAME`

Генерация:

- `COMET_API_KEY`
- `COMET_BASE_URL`
- `KIE_API_KEY`
- `KIE_BASE_URL`
- `KIE_UPLOAD_BASE_URL`
- model env-переменные для Banana/Kling/Seedance при необходимости

Платежи:

- `TBANK_TERMINAL_KEY`
- `TBANK_PASSWORD`
- `TBANK_CALLBACK_URL`
- `TBANK_SUCCESS_URL`
- `TBANK_FAIL_URL`

Не логировать и не коммитить токены, пароли, API-ключи и платежные секреты.

## Запуск

Локально:

```bash
python3 -m app.main
```

С автоперезапуском при изменениях:

```bash
python3 -m app.watchdog
```

Production через systemd:

```bash
systemctl restart stupidbot
systemctl status stupidbot
journalctl -u stupidbot -f
```

## Миграции

Инициализация/обновление БД и дефолтных записей:

```bash
python3 -m scripts.init_db
```

Команда должна выполняться перед запуском сервиса. В systemd-шаблоне это делается через `ExecStartPre`.

## Тесты

Быстрая проверка импортов:

```bash
python3 -m compileall -q app scripts
```

Админка:

```bash
python3 scripts/admin_smoke.py
```

Основная регрессия:

```bash
python3 scripts/regression_500.py
```

Перед деплоем минимум: compileall + admin_smoke + regression_500.

## Структура проекта

```text
app/
  main.py                 FastAPI app, webhook, Mini App API, callbacks
  bot.py                  Bot/Dispatcher, middleware, commands
  config.py               env settings
  db.py                   SQLAlchemy engine/session/init
  models.py               DB models
  plugins/
    core/                 старт, меню, баланс, поддержка
    generation/           image/video FSM и генерации
    payments/             пакеты и оплата
    partners/             партнерка и выводы
    feed/                 публичная лента
    gallery/              галерея
    admin/                админ-панель
  services/
    comet.py              Comet API client
    kie.py                KIE API client
    payments.py           платежная логика
    task_tracker.py       polling/results tracker
scripts/
  init_db.py              миграции/seed
  regression_500.py       регрессия
  admin_smoke.py          smoke админки
systemd/
  stupidbot.service       пример unit-файла
```

## CI

GitHub Actions workflow: `.github/workflows/ci.yml`.

Что запускается:

```bash
python3 -m compileall -q app scripts
python3 scripts/admin_smoke.py
python3 scripts/regression_500.py
```

CI поднимает PostgreSQL 16 и Redis 7 services, задает безопасные test env-переменные и не использует production secrets.

Локально тот же набор проверок:

```bash
scripts/ci.sh
```

## Деплой

1. Обновить код на сервере.
2. Проверить `.env` и секреты.
3. Выполнить миграции:
   ```bash
   python3 -m scripts.init_db
   ```
4. Проверить код:
   ```bash
   python3 -m compileall -q app scripts
   python3 scripts/admin_smoke.py
   python3 scripts/regression_500.py
   ```
5. Перезапустить сервис:
   ```bash
   systemctl restart stupidbot
   systemctl is-active stupidbot
   ```
6. Проверить логи:
   ```bash
   journalctl -u stupidbot --since "5 min ago" --no-pager
   ```

На текущем сервере nginx проксирует `https://stupid.chillcreative.ru` на `127.0.0.1:8092`, поэтому в `.env` должен быть `PORT=8092`.

## Обработка ошибок и UX-сценарии

Пользователь не должен видеть traceback, order_id, токены, ключи или технические ответы провайдеров.

Пользовательский fallback:

```text
Не удалось выполнить операцию. Попробуйте ещё раз через несколько минут.
```

Что предусмотрено:

- отмена FSM через кнопку `Отмена`, текст `отмена` или `/cancel`;
- возврат назад/домой через кнопки и текст `назад`/`главное меню`;
- повторный ввод при неверных данных без сброса шага;
- неверный тип сообщения в админских FSM — просьба отправить текст;
- потеря состояния после перезапуска — понятный ответ и возврат в меню;
- повторное нажатие платежных кнопок — идемпотентная проверка статуса;
- устаревший callback — логируется и не валит webhook;
- удаленное/неизмененное сообщение — fallback на новое сообщение, где безопасно;
- пользователь заблокировал бота — исключение подавляется при уведомлениях;
- внешние API ошибки/таймауты — пользователю общий текст, детали в логах.

## Админ-панель

Администратор может:

- находить пользователей;
- блокировать и разблокировать пользователей;
- менять баланс;
- просматривать заказы/операции генерации;
- повторно проверять зависшие операции по provider task id;
- управлять тарифами и безлимитами;
- управлять публичной галереей и партнерскими ссылками;
- просматривать платежи и вручную подтверждать manual pending заявки;
- обрабатывать обращения через карточку пользователя/баланс/бан/рассылку;
- отправлять уведомления через рассылку;
- смотреть последние ошибки из journal;
- смотреть базовую аналитику.

Повторный запуск операции в админке реализован безопасно как повторная проверка статуса существующей provider-операции. Он не создает новый платный запрос и не списывает кредиты повторно.

## Логирование

Логируется:

- запуск и остановка приложения;
- Telegram ID пользователя;
- выбранное действие/callback/команда;
- начало внешнего платежного запроса;
- время выполнения операций;
- результат операции;
- исключения с traceback;
- платежные события;
- административные действия через callbacks/messages.

Не логировать:

- токены;
- пароли;
- API-ключи;
- платежные секреты;
- персональные данные без необходимости.
