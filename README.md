# WPN Manager

Система автоматической регистрации и ротации аккаунтов **wpn.team**.

Регистрирует аккаунты на pre-зарегистрированных почтах [Atomic Mail](https://atomicmail.io), мониторит подписку, и за 1 день до окончания автоматически создаёт новый аккаунт на следующей свободной почте. Веб-дашборд показывает статус, список аккаунтов и live-лог.

## Возможности

- Регистрация через email + 6-значный код (passwordless auth WPN)
- Чтение кодов из Atomic Mail через headless-браузер (Playwright)
- Автоматический мониторинг подписки (каждые 30 мин)
- Ротация: за N дней до истечения → новый аккаунт
- Авто-refresh access/refresh токенов
- Веб-дашборд: статус, аккаунты, логи в реальном времени (WebSocket)
- Управление: регистрация/ротация/проверка/стоп-демон из браузера

## Установка

Нужны **Python 3.10+** и **Node.js 18+** (для headless-чтения почты).

```bash
# Python-зависимости
pip install -r requirements.txt

# Node-зависимости (Playwright + Chromium)
npm install
npx playwright install chromium
```

## Настройка

1. Скопируй конфиг:

```bash
cp config.yaml.example config.yaml
```

2. Открой `config.yaml` и заполни:
   - `atomic_mails` — список почт в формате **email + пароль** от ящика Atomic Mail
   - остальные поля можно оставить по умолчанию

```yaml
wpn:
  base_url: "https://wpnaccount.com"
  check_interval_minutes: 30
  rotation_threshold_days: 1

atomic_mails:
  - email: "user1@atomicmail.io"
    password: "пароль_от_ящика"
  - email: "user2@atomicmail.io"
    password: "пароль_от_ящика"

storage:
  db_path: "./accounts.db"

logging:
  level: "INFO"
  file: "./wpn_manager.log"
```

### Как работает вход в Atomic Mail

Atomic Mail использует end-to-end шифрование: вход по email+паролю выполняется
через **headless Chromium** (`atomic_reader.mjs`), а не HTTP API. Сессия
кэшируется в `./.atomic_profiles/<local-part>/`, поэтому вход происходит один
раз, последующие запуски переиспользуют cookies.

Ничего отдельно получать не нужно — только email и пароль от ящика.

## Запуск

```bash
python main.py
```

Открой дашборд: **http://127.0.0.1:8000**

Опции:

```bash
python main.py --host 0.0.0.0 --port 8080 -c my_config.yaml
```

## Как это работает

### Регистрация аккаунта

```
1. AtomicMail (headless): baseline() — снимок текущих WPN-писем (чтобы игнорировать старые коды)
2. WPN:       POST /api/proxy/v1/auth/email/code/request { email }     → challenge_id
3. AtomicMail (headless): read_code(exclude=baseline) — ждём СВЕЖЕЕ письмо, извлекаем код
4. WPN:       POST /api/proxy/v1/auth/email/code/exchange { challenge_id, code }  → tokens
5. WPN:       GET  /api/proxy/v1/subscription                           → статус подписки
6. SQLite:    сохраняем аккаунт
```

### Цикл демона

```
каждые 30 мин:
  проверить подписку активного аккаунта
  если дней <= threshold И есть свободные почты:
    отметить старый аккаунт как expiring
    зарегистрировать новый аккаунт
```

### Ротация токенов

Если `access_token` невалиден → `POST /api/proxy/v1/auth/refresh` → обновление в БД.

## Веб-дашборд

| Секция | Описание |
|--------|----------|
| Текущий аккаунт | дней осталось, прогресс-бар, дата истечения |
| Управление | кнопки: Зарегистрировать, Ротация, Проверить, Демон |
| Аккаунты | таблица всех аккаунтов со статусами |
| Лог активности | live-поток через WebSocket |

## Структура проекта

```
wpn-manager/
├── main.py              # точка входа: daemon + uvicorn
├── web.py               # FastAPI: REST + WebSocket
├── account_manager.py   # оркестратор регистрации/ротации
├── wpn_client.py        # WPN REST API клиент
├── atomic_client.py     # обёртка над Node reader (subprocess)
├── atomic_reader.mjs    # headless Playwright: вход + чтение кода
├── storage.py           # SQLite: аккаунты + activity_log
├── static/index.html    # дашборд (Tailwind, тёмная тема)
├── config.yaml.example  # шаблон конфига
├── requirements.txt     # Python-зависимости
└── package.json         # Node-зависимости (Playwright)
```

## Устранение неполадок

| Проблема | Решение |
|----------|---------|
| «Код подтверждения не получен» | Проверь `capability_jwt` в конфиге; письмо могло задержаться |
| «No challenge_id» | WPN API изменился — проверь эндпоинты в `wpn_client.py` |
| Почты кончились | Добавь новые в `config.yaml` → перезапусти |
| Токен просрочен | Авто-refresh должен сработать; если нет — зарегистрируй заново |
| Дашборд не грузится | Проверь что порт свободен; смотри логи в `wpn_manager.log` |
