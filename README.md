# TrustPanel Bot

Telegram-бот для управления клиентами TrustTunnel VPN: добавление/продление/удаление
пользователей, генерация ссылок подключения, авто-напоминания об истечении подписки,
приём чеков об оплате, бэкапы БД, self-hosted деплой через systemd.

Это форк-рефакторинг [оригинального репозитория](https://github.com/danilwarhammer40000/trusttunnel_botpannel) —
см. `CHANGELOG.md` за полным списком отличий и обоснованием каждого изменения.

## Структура

```
bot/
  bot.py               — ЕДИНСТВЕННЫЙ entrypoint: грузит .env, включает все
                          роутеры из handlers/, запускает polling
  config.py            — BOT_TOKEN/ADMIN_ID/TRUSTTUNNEL_DOMAIN + сам объект Bot
  states.py            — все aiogram FSM StatesGroup в одном месте
  keyboards.py         — клавиатуры, общие для нескольких handlers/-файлов
  formatting.py        — форматирование текста карточки/сообщений (без роутера)
  pagination.py        — постраничный вывод списков (paginate/pagination_nav_row)
  display.py           — настройки отображения списков + сортировка + метки 🔔/🔸
  trial.py             — учёт использованных триалов + генерация username/пароля
  access.py            — is_admin/admin_only/run_sync — общее почти для всех хендлеров
  handlers/
    start.py             — /start, триал-регистрация, ❌ Cancel, привязка по карточке
    client_menu.py        — Мой статус, Моя ссылка, Реквизиты, 📖 Инструкция
    feedback.py            — ✉️ Написать администратору (+ проверка "это чек?")
    receipt.py               — приём чека от клиента + апрув/реджект админом
    broadcast.py               — 📢 Рассылка (всем / выбранным / проверка привязок)
    database.py                 — 🗄 База данных (экспорт/импорт/управление триалами)
    sorting.py                   — ⚙️ Сортировка БД
    sync_deploy.py                — 🔄 Sync users / 🚀 Деплой
    add_user.py                    — ➕ Add user (один / несколько)
    list_users.py                   — 📋 List users + меню действий над пользователем
    leader_link.py                   — «👑 Назначить ведущим» (связка мульти-девайс клиентов)
    extend.py                        — раздел "⏳ Extend" из меню действий
    mass_delete.py                    — 🗑 Удаление пользователей
    get_link.py                        — 🔗 Get link
core/
  dates.py             — единая точка правды для парсинга/сравнения дат
  db.py                — JSON-хранилище пользователей (users.json), с file-lock;
                          + синхронизация даты/статуса для связанных ведущий/ведомый
  credentials.py       — генерация credentials.toml для самого туннеля
  generator.py         — генерация ссылки подключения
  service.py           — пересборка credentials + перезапуск trusttunnel.service
  notify.py            — синхронная отправка сообщений/документов через Bot API
  paths.py             — пути ко всем файлам БД
  payment.py           — реквизиты для оплаты + текст "доступ истёк" (редактируются вручную)
  instructions.py      — тексты инструкций (Android/iOS + обход VPN для РФ-сайтов)
  logging_setup.py     — общая конфигурация logging для всех entrypoint'ов
services/
  cleanup.py           — T-7/T-3/T-0 уведомления + отключение просроченных (демон/таймер)
  backup.py            — плановая выгрузка БД в Telegram (таймер)
  post_disable_reminders.py — напоминания +1/+3 дня после отключения (свой таймер)
systemd/               — юниты, устанавливаемые install.sh/deploy.sh
tests/                  — pytest-тесты (в первую очередь на даты — самая хрупкая логика)
install.sh              — первичная установка на чистом сервере
deploy.sh                — обновление уже установленного экземпляра
```

**Кто чем владеет (для FSM-состояний):** каждое состояние (`AddUser.*`,
`ExtendUser.*`, `MassDelete.*` и т.д. — см. `bot/states.py`) обрабатывается
хендлерами ровно в одном файле из `handlers/`. Другой файл может *перейти*
в чужое состояние (например, `list_users.py` включает `AdminMessage.personal`,
а обрабатывает его `feedback.py`) — это нормально и задокументировано в
докстринге каждого файла, где это происходит. Порядок `dp.include_router(...)`
в `bot/bot.py` из-за этого не влияет на поведение: у каждого `callback_data`
и каждого состояния ровно один владелец.

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

- Бот работает от `root` (нужен `systemctl restart` и запись в `/opt/trusttunnel`).
  В `systemd/trustpanel-bot.service` оставлен комментарий, как сузить права,
  если ваша установка TrustTunnel это позволяет.
- Пароли пользователей хранятся в открытом виде (это соответствует модели: сервер
  должен уметь их читать). Файлы БД и credentials.toml теперь принудительно
  получают права `600` при каждой записи.
- Ни один хендлер пока не покрыт end-to-end тестами против живого aiogram
  (только `core/dates.py` и `core/db.py` — см. `tests/`). Для `bot/handlers/`
  структура теперь это позволяет сделать точечно, файл за файлом.
