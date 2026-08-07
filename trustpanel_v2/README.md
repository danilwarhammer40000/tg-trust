# TrustPanel Bot

Telegram-бот для управления клиентами TrustTunnel VPN: добавление/продление/удаление
пользователей, генерация ссылок подключения, авто-напоминания об истечении подписки,
приём чеков об оплате, бэкапы БД, self-hosted деплой через systemd.

Это форк-рефакторинг [оригинального репозитория](https://github.com/danilwarhammer40000/trusttunnel_botpannel) —
см. `CHANGELOG.md` за полным списком отличий и обоснованием каждого изменения.

## Структура

```
bot/bot.py           — вся логика диалогов (aiogram 3, FSM)
core/
  dates.py           — единая точка правды для парсинга/сравнения дат
  db.py              — JSON-хранилище пользователей (users.json), с file-lock
  credentials.py     — генерация credentials.toml для самого туннеля
  generator.py       — генерация ссылки подключения
  service.py         — пересборка credentials + перезапуск trusttunnel.service
  notify.py          — синхронная отправка сообщений/документов через Bot API
  paths.py           — пути ко всем файлам БД
  payment.py         — реквизиты для оплаты (редактируется вручную)
  instructions.py    — тексты инструкций по подключению (Android/iOS)
  logging_setup.py   — общая конфигурация logging для всех entrypoint'ов
services/
  cleanup.py         — T-7/T-3/T-0 уведомления + отключение просроченных (демон/таймер)
  backup.py          — плановая выгрузка БД в Telegram (таймер)
systemd/             — юниты, устанавливаемые install.sh/deploy.sh
tests/                — pytest-тесты (в первую очередь на даты — самая хрупкая логика)
install.sh            — первичная установка на чистом сервере
deploy.sh              — обновление уже установленного экземпляра
```

## Установка

```bash
sudo PROJECT_DIR=/opt/trustpanel bash install.sh
```

Спросит `BOT_TOKEN`, `ADMIN_ID`, `TRUSTTUNNEL_DOMAIN` и поднимет всё через systemd.
Требует, чтобы TrustTunnel уже был установлен в `/opt/trusttunnel`.

## Обновление

```bash
sudo bash /opt/trustpanel/deploy.sh
```

Тянет `git pull`, обновляет зависимости и systemd-юниты (если менялись), перезапускает бота,
шлёт админу сообщение об успехе/неудаче.

## Разработка и тесты

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements-dev.txt
pytest tests/ -v
```

## Переменные окружения

См. `.env.example`. Обязательные: `BOT_TOKEN`, `ADMIN_ID`, `TRUSTTUNNEL_DOMAIN`.

## Известные ограничения / что дальше

- `bot/bot.py` остаётся одним файлом (~1700 строк). Разбиение на aiogram-роутеры
  (`handlers/admin.py`, `handlers/client.py`, ...) — следующий логичный шаг,
  но это отдельная, более рискованная работа, которую лучше делать с реальными
  end-to-end тестами против тестового бота, а не вслепую.
- Бот работает от `root` (нужен `systemctl restart` и запись в `/opt/trusttunnel`).
  В `systemd/trustpanel-bot.service` оставлен комментарий, как сузить права,
  если ваша установка TrustTunnel это позволяет.
- Пароли пользователей хранятся в открытом виде (это соответствует модели: сервер
  должен уметь их читать). Файлы БД и credentials.toml теперь принудительно
  получают права `600` при каждой записи.
