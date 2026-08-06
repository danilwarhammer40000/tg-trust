import asyncio
import logging
import os
import re
import json
import zipfile
import io
import shutil
import secrets
import string
from datetime import datetime, timedelta

from dotenv import load_dotenv
load_dotenv()

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    BufferedInputFile,
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter

from core.logging_setup import setup_logging
from core.service import safe_sync
from core.generator import generate_link
from core.dates import (
    utcnow_naive,
    is_expired,
    calc_new_expiry,
    calc_new_expiry_months,
    add_calendar_months,
    parse_expiry,
)
from core.db import (
    add_user,
    delete_user,
    list_users,
    get_user,
    update_user,
    get_user_by_telegram_id,
    DB_PATH,
)
from core.paths import TRIAL_USED_PATH, SETTINGS_PATH, BACKUP_FILES
from core.payment import PAYMENT_INFO
from core.instructions import (
    render_android_instructions,
    render_ios_instructions,
    MANUAL_CONNECT_STEPS,
)
from services import cleanup as cleanup_service

setup_logging()
log = logging.getLogger(__name__)


# ---------------- ENV ----------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DOMAIN = os.getenv("TRUSTTUNNEL_DOMAIN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")
if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID missing")
if not DOMAIN:
    raise RuntimeError("TRUSTTUNNEL_DOMAIN missing")


# ---------------- BOT ----------------

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ---------------- TRIAL TRACKING (survives user deletion) ----------------
#
# Kept in a separate file from users.json on purpose: if an admin deletes an
# expired trial account (via "🗑 Удаление пользователей"), that shouldn't let
# the same Telegram account register for a second free trial.

def _load_trial_used_ids() -> set:
    try:
        with open(TRIAL_USED_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def _save_trial_used_ids(ids: set):
    os.makedirs(os.path.dirname(TRIAL_USED_PATH), exist_ok=True)
    with open(TRIAL_USED_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f)


def has_used_trial(telegram_id: int) -> bool:
    return telegram_id in _load_trial_used_ids()


def mark_trial_used(telegram_id: int):
    ids = _load_trial_used_ids()
    ids.add(telegram_id)
    _save_trial_used_ids(ids)


def generate_trial_password(length: int = 10) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")

RU_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def slugify_name(name: str) -> str:
    """Transliterates Cyrillic to Latin, then strips anything outside a-z0-9."""
    lowered = name.lower()
    transliterated = "".join(RU_TO_LAT.get(ch, ch) for ch in lowered)
    return re.sub(r"[^a-z0-9]", "", transliterated)


def generate_username_from_name(first_name, tg_username, tg_id) -> str:
    base = slugify_name(first_name) if first_name else ""

    if not base and tg_username:
        base = slugify_name(tg_username)

    if not base:
        base = "user"

    tg_suffix = str(tg_id)

    # Reserve room for "_" + telegram_id, keep total length within the 20-char limit
    max_base_len = 20 - 1 - len(tg_suffix)

    if max_base_len < 1:
        # tg_id itself is already at the limit (not realistic today, but just in case)
        return tg_suffix[-20:]

    base = base[:max_base_len] or "u"

    candidate = f"{base}_{tg_suffix}"

    # telegram_id is unique per account, and has_used_trial() already blocks a
    # second trial from the same account, so a collision here would only
    # happen from a stale/orphaned record — this is just a paranoid fallback.
    if get_user(candidate):
        candidate = f"{base}_{tg_suffix}_{secrets.randbelow(9000) + 1000}"[:20]

    return candidate


# ---------------- FSM ----------------

class AddUser(StatesGroup):
    username = State()
    password = State()
    days = State()
    manual_date = State()


class AddUserMulti(StatesGroup):
    username = State()
    password = State()
    days = State()
    manual_date = State()
    continue_choice = State()
    done_actions = State()


class ExtendUser(StatesGroup):
    mode = State()
    manual = State()


class ReceiptConfirm(StatesGroup):
    waiting = State()


class Feedback(StatesGroup):
    waiting = State()


class MassDelete(StatesGroup):
    select = State()
    confirm = State()


class RenewalApproval(StatesGroup):
    manual_date = State()


class SetTelegramId(StatesGroup):
    waiting = State()


class AdminMessage(StatesGroup):
    personal = State()
    personal_confirm = State()
    broadcast = State()
    broadcast_confirm = State()
    select_recipients = State()
    selective_text = State()
    selective_confirm = State()


# ---------------- ROLE HELPERS ----------------

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


async def admin_only(msg_or_call) -> bool:
    """Returns True if allowed to proceed, otherwise answers politely and returns False."""
    uid = msg_or_call.from_user.id
    if is_admin(uid):
        return True

    if isinstance(msg_or_call, CallbackQuery):
        await msg_or_call.answer("⛔ Недоступно", show_alert=True)
    else:
        await msg_or_call.answer("⛔ Эта команда доступна только администратору.")
    return False


async def run_sync():
    """Run the blocking systemctl-restart sync off the event loop."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, safe_sync)


# ---------------- KEYBOARDS ----------------

main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Add user")],
        [KeyboardButton(text="📋 List users")],
        [KeyboardButton(text="🗑 Удаление пользователей")],
        [KeyboardButton(text="🔗 Get link")],
        [KeyboardButton(text="📢 Рассылка")],
        [KeyboardButton(text="🗄 База данных")],
        [KeyboardButton(text="⚙️ Сортировка БД")],
        [KeyboardButton(text="🔄 Sync users")],
        [KeyboardButton(text="🚀 Деплой")]
    ],
    resize_keyboard=True
)

client_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="ℹ️ Мой статус")],
        [KeyboardButton(text="🔗 Моя ссылка")],
        [KeyboardButton(text="💳 Реквизиты для оплаты")],
        [KeyboardButton(text="✉️ Написать администратору")],
    ],
    resize_keyboard=True
)

cancel_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="❌ Cancel")]],
    resize_keyboard=True
)


# ---------------- MESSAGE FORMATTING ----------------

QR_URL_RE = re.compile(r"(https://trusttunnel\.org/qr\.html#tt=\S+)")

# Matches the connection card the admin sends to clients:
# 👤 Username: SWAnton
# 🔑 Password: SWAnton123
CARD_RE = re.compile(
    r"username\s*:\s*(\S+).*?password\s*:\s*(\S+)",
    re.IGNORECASE | re.DOTALL
)


def looks_like_card(text: str) -> bool:
    """
    Cheap pre-check used as a message filter.

    NOTE: aiogram's F.text.regexp() filter matches from the START of the
    string (re.match semantics), so a pattern like r"username\\s*:" never
    fires on real cards — they start with an emoji ("👤 Username: ..."),
    not the literal word "Username". Using a plain callable filter with
    re.search() avoids that trap entirely.
    """
    if not text:
        return False
    return bool(re.search(r"username\s*:", text, re.IGNORECASE)) and \
        bool(re.search(r"password\s*:", text, re.IGNORECASE))


def extract_qr_link(raw_link: str) -> str:
    """Pulls just the https://trusttunnel.org/qr.html#tt=... URL out of
    generate_link()'s raw output, which otherwise contains multiple formats
    (tt:// scheme + the https:// page) concatenated together."""
    match = QR_URL_RE.search(raw_link)
    return match.group(1) if match else raw_link


def format_connection_message(username: str, password: str, expires_at, raw_link: str) -> str:
    qr_url = extract_qr_link(raw_link)

    return (
        f"👤 Username: {username}\n"
        f"🔑 Password: {password}\n"
        f"⏳ Expires: {expires_at or '∞'}\n\n"
        f"Для подключения перейдите по ссылке 👇\n{qr_url}\n\n"
        f"И нажмите синюю кнопку \"Open in TrustTunnel App\""
    )


def format_full_instructions_message(username: str, password: str, expires_at, raw_link: str) -> str:
    """
    Card + generic post-install connection steps in one continuous message —
    for the manual admin flow (Add user / Get link). App installation itself
    isn't explained here since the admin already walks the client through
    that separately before sending the card.
    """
    card = format_connection_message(username, password, expires_at, raw_link)
    return f"{card}\n\n{MANUAL_CONNECT_STEPS}"


def platform_choice_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 iOS", callback_data="howto:ios")],
        [InlineKeyboardButton(text="🤖 Android", callback_data="howto:android")],
    ])


@dp.callback_query(F.data == "howto:ios")
async def howto_ios(call: CallbackQuery):
    user = get_user_by_telegram_id(call.from_user.id)
    link = extract_qr_link(generate_link(user["username"], DOMAIN)) if user and user.get("username") else None

    await call.message.answer(render_ios_instructions(link))
    await call.answer()


@dp.callback_query(F.data == "howto:android")
async def howto_android(call: CallbackQuery):
    user = get_user_by_telegram_id(call.from_user.id)
    link = extract_qr_link(generate_link(user["username"], DOMAIN)) if user and user.get("username") else None

    await call.message.answer(render_android_instructions(link))
    await call.answer()


def user_button_label(u: dict) -> str:
    username = u.get("username", "?")
    expires_at = u.get("expires_at")
    label = f"{username} ({expires_at or '∞'})"

    if u.get("telegram_id"):
        label = f"🔔 {label}"

    return f"🔸 {label}" if is_expired(expires_at) else label


def _expiry_sort_key(u: dict):
    """
    Mirrors core.db._sort_key's date ordering: unlimited (no expiry) first,
    then furthest-expiring first, nearest-expiring last, broken dates at
    the very end.
    """
    expires_at = u.get("expires_at")

    if not expires_at:
        return (0, 0)

    d = parse_expiry(expires_at)
    if d is None:
        return (2, 0)

    return (1, -d.date().toordinal())


# ---------------- SETTINGS (persistent, e.g. list grouping toggle) ----------------

DEFAULT_SETTINGS = {"group_by_subscription": True, "hide_unlimited": False}


def _load_settings() -> dict:
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {**DEFAULT_SETTINGS, **data}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULT_SETTINGS)


def _save_settings(settings: dict):
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f)


def is_grouping_enabled() -> bool:
    return _load_settings().get("group_by_subscription", True)


def toggle_grouping() -> bool:
    settings = _load_settings()
    settings["group_by_subscription"] = not settings.get("group_by_subscription", True)
    _save_settings(settings)
    return settings["group_by_subscription"]


def is_hide_unlimited_enabled() -> bool:
    return _load_settings().get("hide_unlimited", False)


def toggle_hide_unlimited() -> bool:
    settings = _load_settings()
    settings["hide_unlimited"] = not settings.get("hide_unlimited", False)
    _save_settings(settings)
    return settings["hide_unlimited"]


def prepare_users_for_display(users: list) -> list:
    """
    Applies both display settings, in order: optional hide-unlimited filter,
    then sort (grouped-by-subscription + date, or plain date-only — see
    sorted_users_for_display). Use this everywhere a full user list gets
    rendered as buttons, instead of calling list_users() directly.
    """
    if is_hide_unlimited_enabled():
        users = [u for u in users if u.get("expires_at")]
    return sorted_users_for_display(users)


def sorted_users_for_display(users: list) -> list:
    """
    If grouping is enabled: subscribed (🔔, telegram_id set) users above
    unsubscribed ones. Either way, keeps the same expiry-date ordering
    list_users() already provides as the (secondary, or only) sort key.
    """
    if is_grouping_enabled():
        return sorted(
            users,
            key=lambda u: (0 if u.get("telegram_id") else 1, *_expiry_sort_key(u))
        )
    return sorted(users, key=_expiry_sort_key)


# ---------------- PAGINATION ----------------
#
# Telegram inline keyboards silently break past a certain size (the client
# either refuses to render the message's reply_markup, or truncates it) —
# with enough clients, one row per user meant the list just cut off partway
# through with no error, no scroll, nothing. Every "one row per user" list
# in this file (List users, Get link, mass delete, broadcast recipient
# picker, trial management) goes through this helper instead of dumping
# every row into one InlineKeyboardMarkup.

USERS_PAGE_SIZE = 15


def paginate(items: list, page: int):
    """Returns (page_items, total_pages, page) — page is clamped into range."""
    total_pages = max(1, (len(items) + USERS_PAGE_SIZE - 1) // USERS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    start = page * USERS_PAGE_SIZE
    page_items = items[start:start + USERS_PAGE_SIZE]

    return page_items, total_pages, page


def pagination_nav_row(page: int, total_pages: int, callback_prefix: str) -> list:
    """
    Builds the ⬅️/➡️ row for a given page. callback_prefix's handler must
    accept a trailing ":{page}" and re-render the same list at that page —
    see e.g. F.data.startswith("listpage:").
    """
    if total_pages <= 1:
        return []

    row = []
    if page > 0:
        row.append(InlineKeyboardButton(text="⬅️ Пред.", callback_data=f"{callback_prefix}:{page - 1}"))
    row.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        row.append(InlineKeyboardButton(text="След. ➡️", callback_data=f"{callback_prefix}:{page + 1}"))

    return [row]


@dp.callback_query(F.data == "noop")
async def noop_callback(call: CallbackQuery):
    """The "3/7" page-indicator button in the middle of the nav row — not clickable."""
    await call.answer()


def renewal_admin_kb(username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕1 мес", callback_data=f"apr:{username}:30"),
            InlineKeyboardButton(text="➕2 мес", callback_data=f"apr:{username}:60"),
        ],
        [InlineKeyboardButton(text="✍️ Ручная дата", callback_data=f"apr:{username}:manual")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"apr:{username}:reject")]
    ])


# ---------------- START ----------------

@dp.message(F.text == "/start")
async def start(msg: Message):
    if is_admin(msg.from_user.id):
        await msg.answer("TrustPanel online", reply_markup=main_menu)
        return

    user = get_user_by_telegram_id(msg.from_user.id)
    if user:
        await msg.answer(
            f"👋 С возвращением, {user.get('username')}!",
            reply_markup=client_menu
        )
        return

    if has_used_trial(msg.from_user.id):
        await msg.answer(
            "👋 Привет! Чтобы я мог присылать вам уведомления об истечении доступа,\n"
            "пришлите сюда вашу карточку подключения целиком "
            "(то сообщение с Username / Password, которое вам отправил администратор)."
        )
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, пользуюсь — привязать карточку", callback_data="onboard:existing")],
        [InlineKeyboardButton(text="🆕 Нет — попробовать 4 дня бесплатно", callback_data="onboard:trial")],
    ])

    await msg.answer(
        "👋 Привет! Вы уже пользуетесь клубным TrustTunnel VPN?",
        reply_markup=kb
    )


@dp.callback_query(F.data == "onboard:existing")
async def onboard_existing(call: CallbackQuery):
    await call.message.answer(
        "Пришлите сюда вашу карточку подключения целиком "
        "(то сообщение с Username / Password, которое вам отправил администратор)."
    )
    await call.answer()


@dp.callback_query(F.data == "onboard:trial")
async def onboard_trial_start(call: CallbackQuery):
    if has_used_trial(call.from_user.id):
        await call.answer("Пробный период уже был использован этим аккаунтом.", show_alert=True)
        return

    username = generate_username_from_name(
        call.from_user.first_name, call.from_user.username, call.from_user.id
    )
    password = generate_trial_password()
    expires_at = (utcnow_naive() + timedelta(days=4)).strftime("%Y-%m-%d")

    add_user({
        "username": username,
        "password": password,
        "created_at": utcnow_naive().strftime("%Y-%m-%d"),
        "expires_at": expires_at,
        "status": "active",
        "telegram_id": call.from_user.id,
        "notified_days": [],
        "pending_request": None,
    })

    mark_trial_used(call.from_user.id)

    await run_sync()

    await call.message.answer(
        f"🎁 Пробный доступ на 4 дня активирован! Доступ до {expires_at}.\n\n"
        f"👤 Username: {username}\n"
        f"🔑 Password: {password}\n"
        f"⏳ Expires: {expires_at}\n\n"
        "Об истечении напомню заранее, а продлить сможете прямо в этом чате.",
        reply_markup=client_menu
    )

    await call.message.answer(
        "📖 Осталось настроить подключение — выберите вашу платформу:",
        reply_markup=platform_choice_kb()
    )

    await bot.send_message(
        ADMIN_ID,
        f"🆕 Новая регистрация по триалу: {username} (tg id {call.from_user.id}), доступ до {expires_at}."
    )

    await call.answer()


# ---------------- CANCEL ----------------

@dp.message(F.text.lower() == "❌ cancel")
async def cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("❌ Cancelled", reply_markup=ReplyKeyboardRemove())
    if is_admin(msg.from_user.id):
        await msg.answer("Menu:", reply_markup=main_menu)
    else:
        await msg.answer("Menu:", reply_markup=client_menu)


# ---------------- CLIENT: STATUS ----------------

@dp.message(F.text == "ℹ️ Мой статус")
async def client_status(msg: Message):
    user = get_user_by_telegram_id(msg.from_user.id)
    if not user:
        await msg.answer("Вы ещё не привязаны. Пришлите вашу карточку подключения (Username/Password).")
        return

    expires_at = user.get("expires_at")
    status_line = "∞ бессрочно" if not expires_at else expires_at
    await msg.answer(f"👤 {user.get('username')}\n⏳ Доступ до: {status_line}")


@dp.message(F.text == "💳 Реквизиты для оплаты")
async def client_payment_info(msg: Message):
    if is_admin(msg.from_user.id):
        return
    await msg.answer(PAYMENT_INFO)


@dp.message(F.text == "🔗 Моя ссылка")
async def client_my_link(msg: Message):
    user = get_user_by_telegram_id(msg.from_user.id)
    if not user:
        await msg.answer("Вы ещё не привязаны. Пришлите вашу карточку подключения (Username/Password).")
        return

    username = user.get("username")
    link = generate_link(username, DOMAIN)

    await msg.answer(
        format_connection_message(username, user.get("password"), user.get("expires_at"), link)
    )


# ---------------- CLIENT: BIND BY CARD ----------------

@dp.message(
    StateFilter(None),
    F.from_user.id != ADMIN_ID,
    F.text.func(looks_like_card),
)
async def bind_by_card(msg: Message, state: FSMContext):
    match = CARD_RE.search(msg.text)
    if not match:
        await msg.answer(
            "Не смог распознать карточку. Пришлите её целиком, без изменений, "
            "так как её отправил администратор."
        )
        return

    username, password = match.group(1), match.group(2)
    user = get_user(username)

    if not user or user.get("password") != password:
        await msg.answer("Не нашёл такой аккаунт. Проверьте, что скопировали карточку без изменений.")
        return

    update_user(username, telegram_id=msg.from_user.id)

    await msg.answer(
        f"✅ Готово, {username}! Теперь я буду присылать уведомления об истечении доступа.",
        reply_markup=client_menu
    )

    tg_username = f"@{msg.from_user.username}" if msg.from_user.username else "(без username)"
    await bot.send_message(
        ADMIN_ID,
        f"🔔 Клиент {username} привязал Telegram {tg_username} (id {msg.from_user.id}) — подписан на уведомления."
    )


# ---------------- CLIENT: FEEDBACK TO ADMIN ----------------

@dp.message(F.text == "✉️ Написать администратору")
async def client_feedback_start(msg: Message, state: FSMContext):
    if is_admin(msg.from_user.id):
        return

    user = get_user_by_telegram_id(msg.from_user.id)
    if not user:
        await msg.answer("Вы ещё не привязаны. Пришлите вашу карточку подключения (Username/Password).")
        return

    await state.set_state(Feedback.waiting)
    await msg.answer("Напишите сообщение администратору одним сообщением:", reply_markup=cancel_kb)


@dp.message(Feedback.waiting)
async def client_feedback_send(msg: Message, state: FSMContext):
    user = get_user_by_telegram_id(msg.from_user.id)
    await state.clear()

    if not user:
        await msg.answer("Не удалось определить ваш аккаунт.", reply_markup=client_menu)
        return

    text = msg.text or "[сообщение без текста — фото/файл]"
    username = user.get("username")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Ответить", callback_data=f"reply:{username}")]
    ])

    await bot.send_message(
        ADMIN_ID,
        f"✉️ Обращение от {username} (tg id: {msg.from_user.id}):\n\n{text}",
        reply_markup=kb
    )

    await msg.answer("✅ Отправлено администратору.", reply_markup=client_menu)


# ---------------- ADMIN: PERSONAL MESSAGE (reply to feedback OR "📞 Call") ----------------

@dp.callback_query(F.data.startswith("reply:"))
async def reply_start(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    username = call.data.split(":", 1)[1]

    await state.update_data(target_username=username)
    await state.set_state(AdminMessage.personal)

    await call.message.answer(f"Введите сообщение для {username}:", reply_markup=cancel_kb)
    await call.answer()


@dp.message(AdminMessage.personal)
async def personal_message_preview(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return

    data = await state.get_data()
    username = data.get("target_username")

    if not msg.text:
        await msg.answer("Пришлите текстовое сообщение.")
        return

    user = get_user(username) if username else None

    if not user or not user.get("telegram_id"):
        await state.clear()
        await msg.answer(f"⚠️ У {username} нет привязанного Telegram — сообщение не отправлено.", reply_markup=main_menu)
        return

    await state.update_data(text=msg.text)
    await state.set_state(AdminMessage.personal_confirm)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отправить", callback_data="personal:send")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="personal:cancel")],
    ])

    await msg.answer(
        f"Получатель: {username}\n\nТекст сообщения:\n\n{msg.text}\n\nОтправляем?",
        reply_markup=kb
    )


@dp.callback_query(F.data.startswith("personal:"), AdminMessage.personal_confirm)
async def personal_message_confirm(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    action = call.data.split(":", 1)[1]
    data = await state.get_data()
    username = data.get("target_username")
    text = data.get("text")
    await state.clear()

    if action == "cancel":
        await call.message.answer("Отменено.", reply_markup=main_menu)
        await call.answer()
        return

    user = get_user(username) if username else None

    if not user or not user.get("telegram_id"):
        await call.message.answer(f"⚠️ У {username} нет привязанного Telegram — сообщение не отправлено.", reply_markup=main_menu)
        await call.answer()
        return

    try:
        await bot.send_message(user["telegram_id"], f"✉️ Сообщение от администратора:\n\n{text}")
        await call.message.answer(f"✅ Отправлено {username}.", reply_markup=main_menu)
    except Exception as e:
        log.warning("failed to send personal message to %s: %s", username, e)
        await call.message.answer(f"❌ Не удалось отправить {username}: {e}", reply_markup=main_menu)

    await call.answer()


# ---------------- ADMIN: BROADCAST ----------------

def recipient_button_label(u: dict, selected: set) -> str:
    username = u.get("username", "?")
    expires_at = u.get("expires_at")
    mark = "☑️" if username in selected else "⬜"
    return f"{mark} {username} ({expires_at or '∞'})"


def build_recipient_kb(users: list, selected: set, page: int) -> tuple:
    page_users, total_pages, page = paginate(users, page)

    rows = [
        [InlineKeyboardButton(
            text=recipient_button_label(u, selected),
            callback_data=f"selrecipient:{u['username']}"
        )]
        for u in page_users if u.get("username")
    ]
    rows += pagination_nav_row(page, total_pages, "selrecipientpage")
    rows.append([
        InlineKeyboardButton(text=f"✅ Готово ({len(selected)})", callback_data="selrecipients:done"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="selrecipients:cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows), total_pages, page


def recipient_picker_label(total: int, page: int, total_pages: int) -> str:
    suffix = f", стр. {page + 1}/{total_pages}" if total_pages > 1 else ""
    return f"Выберите получателей ({total} всего{suffix}), тап переключает ☑️/⬜, затем «Готово»:"


@dp.message(F.text == "📢 Рассылка")
async def broadcast_menu(msg: Message):
    if not await admin_only(msg):
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Всем", callback_data="bcast_mode:all")],
        [InlineKeyboardButton(text="🎯 Выбрать получателей", callback_data="bcast_mode:select")],
        [InlineKeyboardButton(text="🔍 Проверить привязки", callback_data="bcast_mode:check")],
    ])
    await msg.answer("Кому отправить рассылку?", reply_markup=kb)


@dp.callback_query(F.data == "bcast_mode:check")
async def broadcast_mode_check(call: CallbackQuery):
    if not await admin_only(call):
        return

    await call.message.answer("🔄 Проверяю привязки (без отправки сообщений)...")
    await call.answer()

    report = await run_binding_check()
    await call.message.answer(report, reply_markup=main_menu)


@dp.callback_query(F.data == "bcast_mode:all")
async def broadcast_mode_all(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    await state.set_state(AdminMessage.broadcast)
    await call.message.answer(
        "Введите текст рассылки — уйдёт всем клиентам с привязанным Telegram:",
        reply_markup=cancel_kb
    )
    await call.answer()


@dp.callback_query(F.data == "bcast_mode:select")
async def broadcast_mode_select(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    users = prepare_users_for_display([u for u in (list_users() or []) if u.get("telegram_id") and u.get("username")])
    if not users:
        await call.message.answer("Нет клиентов с привязанным Telegram.")
        await call.answer()
        return

    await state.set_state(AdminMessage.select_recipients)
    await state.update_data(selected=[], page=0)

    kb, total_pages, page = build_recipient_kb(users, set(), 0)
    await call.message.answer(recipient_picker_label(len(users), page, total_pages), reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data.startswith("selrecipientpage:"), AdminMessage.select_recipients)
async def recipient_picker_page(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    page = int(call.data.split(":", 1)[1])
    await state.update_data(page=page)

    data = await state.get_data()
    selected = set(data.get("selected", []))
    users = prepare_users_for_display([u for u in (list_users() or []) if u.get("telegram_id") and u.get("username")])

    kb, total_pages, page = build_recipient_kb(users, selected, page)
    try:
        await call.message.edit_text(recipient_picker_label(len(users), page, total_pages), reply_markup=kb)
    except Exception:
        pass
    await call.answer()


@dp.callback_query(F.data.startswith("selrecipient:"), AdminMessage.select_recipients)
async def toggle_recipient(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    username = call.data.split(":", 1)[1]

    data = await state.get_data()
    selected = set(data.get("selected", []))
    page = data.get("page", 0)

    if username in selected:
        selected.discard(username)
    else:
        selected.add(username)

    await state.update_data(selected=list(selected))

    users = prepare_users_for_display([u for u in (list_users() or []) if u.get("telegram_id") and u.get("username")])

    try:
        kb, total_pages, page = build_recipient_kb(users, selected, page)
        await call.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass  # "message is not modified" if tapped same state twice quickly — harmless

    await call.answer()


@dp.callback_query(F.data == "selrecipients:cancel", AdminMessage.select_recipients)
async def cancel_recipient_selection(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    await state.clear()
    await call.message.answer("Отменено.", reply_markup=main_menu)
    await call.answer()


@dp.callback_query(F.data == "selrecipients:done", AdminMessage.select_recipients)
async def confirm_recipient_selection(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    data = await state.get_data()
    selected = data.get("selected", [])

    if not selected:
        await call.answer("Выберите хотя бы одного получателя", show_alert=True)
        return

    await state.set_state(AdminMessage.selective_text)

    names = ", ".join(selected)
    await call.message.answer(
        f"Получатели ({len(selected)}): {names}\n\nВведите сообщение для них:",
        reply_markup=cancel_kb
    )
    await call.answer()


@dp.message(AdminMessage.selective_text)
async def selective_message_preview(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return

    if not msg.text:
        await msg.answer("Пришлите текстовое сообщение.")
        return

    data = await state.get_data()
    selected = data.get("selected", [])

    await state.update_data(text=msg.text)
    await state.set_state(AdminMessage.selective_confirm)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Отправить ({len(selected)} чел.)", callback_data="selective:send")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="selective:cancel")],
    ])

    names = ", ".join(selected)
    await msg.answer(
        f"Получатели ({len(selected)}): {names}\n\nТекст сообщения:\n\n{msg.text}\n\nОтправляем?",
        reply_markup=kb
    )


@dp.callback_query(F.data.startswith("selective:"), AdminMessage.selective_confirm)
async def selective_message_confirm(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    action = call.data.split(":", 1)[1]
    data = await state.get_data()
    selected = data.get("selected", [])
    text = data.get("text")
    await state.clear()

    if action == "cancel":
        await call.message.answer("Отменено.", reply_markup=main_menu)
        await call.answer()
        return

    sent = 0
    failures = []  # (username, reason)

    for username in selected:
        user = get_user(username)
        if user and user.get("telegram_id"):
            try:
                await bot.send_message(user["telegram_id"], text)
                sent += 1
            except Exception as e:
                log.warning("selective broadcast failed for %s: %s", username, e)
                failures.append((username, str(e)))
        else:
            failures.append((username, failure_reason(user)))
        await asyncio.sleep(0.05)

    await call.message.answer(format_send_report(sent, failures), reply_markup=main_menu)
    await call.answer()


@dp.message(AdminMessage.broadcast)
async def broadcast_preview(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return

    await state.update_data(broadcast_text=msg.text)
    await state.set_state(AdminMessage.broadcast_confirm)

    users = list_users() or []
    recipients = [u for u in users if u.get("telegram_id")]

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Отправить ({len(recipients)} чел.)", callback_data="bcast:send")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="bcast:cancel")]
    ])

    await msg.answer(
        f"Получателей: {len(recipients)}\n\nТекст:\n{msg.text}\n\nОтправляем?",
        reply_markup=kb
    )


def format_send_report(sent: int, failures: list) -> str:
    """
    failures: list of (username, reason) tuples.
    """
    report = f"✅ Отправлено: {sent}\n❌ Ошибок: {len(failures)}"

    if failures:
        MAX_SHOWN = 20
        lines = [f"• {name}: {reason}" for name, reason in failures[:MAX_SHOWN]]
        report += "\n\n" + "\n".join(lines)

        remaining = len(failures) - MAX_SHOWN
        if remaining > 0:
            report += f"\n… и ещё {remaining}"

    return report


def failure_reason(user) -> str:
    if not user:
        return "пользователь не найден"
    if not user.get("telegram_id"):
        return "нет привязанного Telegram"
    return "ошибка отправки"


@dp.callback_query(F.data.startswith("bcast:"), AdminMessage.broadcast_confirm)
async def broadcast_confirm(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    action = call.data.split(":")[1]

    if action == "cancel":
        await state.clear()
        await call.message.answer("Отменено.", reply_markup=main_menu)
        await call.answer()
        return

    data = await state.get_data()
    text = data.get("broadcast_text", "")
    await state.clear()

    users = list_users() or []
    recipients = [u for u in users if u.get("telegram_id")]

    sent = 0
    failures = []  # (username, reason)

    await call.message.answer(f"🔄 Отправка {len(recipients)} сообщениям...")

    for u in recipients:
        try:
            await bot.send_message(u["telegram_id"], text)
            sent += 1
        except Exception as e:
            log.warning("broadcast failed for %s: %s", u.get("username"), e)
            failures.append((u.get("username", "?"), str(e)))
        await asyncio.sleep(0.05)  # мягкий троттлинг, чтобы не упереться в лимиты Telegram

    await call.message.answer(format_send_report(sent, failures), reply_markup=main_menu)
    await call.answer()


# ---------------- CLIENT: RECEIPT / PHOTO CATCH-ALL ----------------

@dp.message(StateFilter(None), F.from_user.id != ADMIN_ID, F.photo | F.document)
async def any_media_received(msg: Message, state: FSMContext):
    user = get_user_by_telegram_id(msg.from_user.id)
    if not user:
        await msg.answer(
            "Я вас пока не узнал 🤔\n"
            "Сначала пришлите вашу карточку подключения (текст с Username/Password), "
            "чтобы я мог связать вас с аккаунтом."
        )
        return

    is_photo = bool(msg.photo)
    file_id = msg.photo[-1].file_id if is_photo else msg.document.file_id

    await state.update_data(
        receipt_file_id=file_id,
        receipt_username=user["username"],
        receipt_is_photo=is_photo,
    )
    await state.set_state(ReceiptConfirm.waiting)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, отправить", callback_data="receipt:yes")],
        [InlineKeyboardButton(text="❌ Нет, это не то", callback_data="receipt:no")]
    ])

    await msg.answer("📎 Это чек на продление? Отправляем администратору на проверку?", reply_markup=kb)


@dp.callback_query(F.data == "receipt:yes", ReceiptConfirm.waiting)
async def receipt_yes(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    file_id = data.get("receipt_file_id")
    username = data.get("receipt_username")
    is_photo = data.get("receipt_is_photo", True)

    update_user(username, pending_request={
        "type": "renewal",
        "receipt_file_id": file_id,
        "receipt_is_photo": is_photo,
        "requested_at": utcnow_naive().isoformat()
    })

    user = get_user(username) or {}
    current_expiry = user.get("expires_at")
    expiry_line = current_expiry or "∞ (безлимит)"

    if current_expiry and is_expired(current_expiry):
        expiry_line += " (уже истёк)"

    caption = (
        f"📥 Заявка на продление от {username}\n"
        f"⏳ Текущая дата истечения: {expiry_line}"
    )
    kb = renewal_admin_kb(username)

    if is_photo:
        await bot.send_photo(ADMIN_ID, photo=file_id, caption=caption, reply_markup=kb)
    else:
        await bot.send_document(ADMIN_ID, document=file_id, caption=caption, reply_markup=kb)

    await call.message.answer("✅ Отправлено администратору. Ждите подтверждения.")
    await state.clear()
    await call.answer()


@dp.callback_query(F.data == "receipt:no", ReceiptConfirm.waiting)
async def receipt_no(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("Хорошо, отменил. Если это всё же чек — пришлите его ещё раз.")
    await call.answer()


@dp.message(ReceiptConfirm.waiting, F.photo | F.document)
async def media_while_waiting_confirm(msg: Message):
    await msg.answer(
        "У вас уже есть чек, ожидающий подтверждения ⬆️\n"
        "Сначала нажмите «✅ Да, отправить» или «❌ Нет, это не то» на предыдущем сообщении."
    )


# ---------------- ADMIN: APPROVE / REJECT RENEWAL ----------------

@dp.callback_query(F.data.startswith("apr:"))
async def approve_renewal(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    _, username, action = call.data.split(":")
    user = get_user(username)

    if not user:
        await call.answer("User not found", show_alert=True)
        return

    if action == "reject":
        update_user(username, pending_request=None)
        await call.message.edit_caption(caption=f"❌ Заявка {username} отклонена")

        if user.get("telegram_id"):
            await bot.send_message(
                user["telegram_id"],
                "❌ Чек не подтверждён администратором. Свяжитесь для уточнения."
            )

        await call.answer()
        return

    if action == "manual":
        await state.set_state(RenewalApproval.manual_date)
        await state.update_data(target_username=username)
        await call.message.answer(
            f"Введите новую дату истечения для {username} (YYYY-MM-DD):",
            reply_markup=cancel_kb
        )
        await call.answer()
        return

    # action here is "30" (1 мес) or "60" (2 мес) — both mean calendar months,
    # not a literal day count (see renewal_admin_kb).
    months = int(action) // 30
    was_expired_or_inactive = user.get("status") != "active" or is_expired(user.get("expires_at"))
    new_expires = calc_new_expiry_months(user.get("expires_at"), months)

    update_user(
        username,
        expires_at=new_expires,
        status="active",
        pending_request=None,
        notified_days=[]
    )

    # Only resync + restart trusttunnel if this user was actually missing from
    # credentials.toml (expired/inactive). If they were already active, nothing
    # in credentials.toml changes, and restarting would needlessly drop every
    # other connected client.
    if was_expired_or_inactive:
        await run_sync()

    await call.message.edit_caption(caption=f"✅ {username} продлён до {new_expires}")

    if user.get("telegram_id"):
        await bot.send_message(
            user["telegram_id"],
            f"✅ Ваша подписка продлена до {new_expires}. Спасибо!"
        )

    await call.answer("Готово")


@dp.message(RenewalApproval.manual_date)
async def approve_renewal_manual_date(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return

    data = await state.get_data()
    username = data.get("target_username")
    await state.clear()

    try:
        new_expires = datetime.strptime(msg.text.strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        await msg.answer("Неверный формат даты. Используйте YYYY-MM-DD.", reply_markup=main_menu)
        return

    user = get_user(username)
    if not user:
        await msg.answer("Пользователь не найден.", reply_markup=main_menu)
        return

    was_expired_or_inactive = user.get("status") != "active" or is_expired(user.get("expires_at"))

    update_user(
        username,
        expires_at=new_expires,
        status="active",
        pending_request=None,
        notified_days=[]
    )

    if was_expired_or_inactive:
        await run_sync()

    if user.get("telegram_id"):
        await bot.send_message(
            user["telegram_id"],
            f"✅ Ваша подписка продлена до {new_expires}. Спасибо!"
        )

    await msg.answer(f"✅ {username} продлён до {new_expires}", reply_markup=main_menu)


# ---------------- DATABASE MENU ----------------

def build_db_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, path in BACKUP_FILES.items():
            if os.path.exists(path):
                zf.write(path, arcname=arcname)
    buf.seek(0)
    return buf.read()


def extract_telegram_id_from_message(msg: Message):
    if msg.forward_from:
        return msg.forward_from.id
    if msg.text and msg.text.strip().lstrip("-").isdigit():
        return int(msg.text.strip())
    return None


class DBImport(StatesGroup):
    waiting = State()


@dp.message(F.text == "🗄 База данных")
async def db_menu(msg: Message):
    if not await admin_only(msg):
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Выгрузить сейчас", callback_data="db:export")],
        [InlineKeyboardButton(text="📥 Загрузить БД", callback_data="db:import")],
        [InlineKeyboardButton(text="🎟 Управление триалами", callback_data="db:trials")],
    ])
    await msg.answer("🗄 База данных:", reply_markup=kb)


@dp.callback_query(F.data == "db:export")
async def db_export(call: CallbackQuery):
    if not await admin_only(call):
        return

    data = build_db_zip()
    filename = f"trustpanel_backup_{utcnow_naive().strftime('%Y%m%d_%H%M%S')}.zip"
    doc = BufferedInputFile(data, filename=filename)

    await call.message.answer_document(
        doc,
        caption=f"🗄 Текущая база данных: {', '.join(BACKUP_FILES.keys())}"
    )
    await call.answer()


@dp.callback_query(F.data == "db:import")
async def db_import_start(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    await state.set_state(DBImport.waiting)
    await call.message.answer(
        "📥 Пришлите zip-пакет с бэкапом (файлом — тот, что бот присылал ранее). "
        "Текущие файлы БД будут сохранены рядом с суффиксом .before_restore на всякий случай.",
        reply_markup=cancel_kb
    )
    await call.answer()


def _validate_users_json(content: bytes) -> bool:
    """
    CHANGED: db_import_apply used to only check json.loads() succeeds, which
    accepts *any* valid JSON (e.g. `{}` or `"hello"`) and would silently wipe
    out users.json with garbage. This checks the actual expected shape: a
    list of dicts each with at least a username.
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return False

    if not isinstance(data, list):
        return False

    return all(isinstance(item, dict) and item.get("username") for item in data)


@dp.message(DBImport.waiting, F.document)
async def db_import_apply(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return

    await state.clear()

    doc = msg.document
    if not doc.file_name or not doc.file_name.lower().endswith(".zip"):
        await msg.answer("Это не .zip файл. Загрузка отменена.", reply_markup=main_menu)
        return

    tg_file = await bot.get_file(doc.file_id)
    downloaded = await bot.download_file(tg_file.file_path)
    raw = downloaded.read()

    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
        names = zf.namelist()
    except zipfile.BadZipFile:
        await msg.answer("Файл повреждён или это не zip-архив.", reply_markup=main_menu)
        return

    # safety copy of whatever's currently on disk, in case this restore is a mistake
    for dest in BACKUP_FILES.values():
        if os.path.exists(dest):
            try:
                shutil.copy(dest, dest + ".before_restore")
            except OSError as e:
                log.warning("pre-restore backup failed for %s: %s", dest, e)

    restored = []
    for arcname, dest in BACKUP_FILES.items():
        if arcname not in names:
            continue
        content = zf.read(arcname)

        # users.json gets the stricter shape check (see _validate_users_json);
        # trial_used.json / settings.json only need to be valid JSON.
        if arcname == "users.json":
            valid = _validate_users_json(content)
        else:
            try:
                json.loads(content)
                valid = True
            except json.JSONDecodeError:
                valid = False

        if not valid:
            await msg.answer(f"⚠️ {arcname} в архиве повреждён или неожиданного формата, пропущен.")
            continue

        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(content)
        os.chmod(dest, 0o600)
        restored.append(arcname)

    if not restored:
        await msg.answer(
            f"В архиве не нашлось ни одного нужного файла ({', '.join(BACKUP_FILES.keys())}).",
            reply_markup=main_menu
        )
        return

    await msg.answer(f"✅ Восстановлено: {', '.join(restored)}. Пересобираю credentials и перезапускаю туннель...")

    await run_sync()

    await msg.answer("✅ Готово — туннель синхронизирован с восстановленной БД.", reply_markup=main_menu)


def trial_used_label(tg_id: int) -> str:
    user = get_user_by_telegram_id(tg_id)
    if user and user.get("username"):
        return f"{user['username']} (id {tg_id})"
    return f"id {tg_id} (аккаунт не найден в БД)"


@dp.callback_query(F.data == "db:trials")
async def db_trials_menu(call: CallbackQuery):
    if not await admin_only(call):
        return

    count = len(_load_trial_used_ids())

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить (заблокировать триал)", callback_data="db:trial_add_list")],
        [InlineKeyboardButton(text="➖ Убрать (разрешить новый триал)", callback_data="db:trial_remove_list")],
    ])
    await call.message.answer(f"🎟 Использовано триалов: {count}", reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data == "db:trial_remove_list")
async def db_trial_remove_list(call: CallbackQuery):
    if not await admin_only(call):
        return

    ids = sorted(_load_trial_used_ids())
    if not ids:
        await call.message.answer("Список использовавших триал пуст.")
        await call.answer()
        return

    await render_trial_remove_list(call, ids, 0, edit=False)
    await call.answer()


async def render_trial_remove_list(call: CallbackQuery, ids: list, page: int, edit: bool):
    page_ids, total_pages, page = paginate(ids, page)

    rows = [
        [InlineKeyboardButton(text=trial_used_label(tg_id), callback_data=f"trialdel:{tg_id}")]
        for tg_id in page_ids
    ]
    rows += pagination_nav_row(page, total_pages, "trialdelpage")

    suffix = f", стр. {page + 1}/{total_pages}" if total_pages > 1 else ""
    label = f"Выберите, кому разрешить новый триал ({len(ids)} всего{suffix}):"

    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    if edit:
        try:
            await call.message.edit_text(label, reply_markup=kb)
        except Exception:
            pass
    else:
        await call.message.answer(label, reply_markup=kb)


@dp.callback_query(F.data.startswith("trialdelpage:"))
async def db_trial_remove_page(call: CallbackQuery):
    if not await admin_only(call):
        return

    page = int(call.data.split(":", 1)[1])
    ids = sorted(_load_trial_used_ids())

    await render_trial_remove_list(call, ids, page, edit=True)
    await call.answer()


@dp.callback_query(F.data.startswith("trialdel:"))
async def db_trial_remove_execute(call: CallbackQuery):
    if not await admin_only(call):
        return

    tg_id = int(call.data.split(":", 1)[1])
    ids = _load_trial_used_ids()

    if tg_id in ids:
        ids.discard(tg_id)
        _save_trial_used_ids(ids)
        await call.message.answer(f"✅ id {tg_id} удалён из списка — сможет получить новый триал.", reply_markup=main_menu)
    else:
        await call.message.answer("Уже не в списке.", reply_markup=main_menu)

    await call.answer()


@dp.callback_query(F.data == "db:trial_add_list")
async def db_trial_add_list(call: CallbackQuery):
    if not await admin_only(call):
        return

    users = [u for u in (list_users() or []) if u.get("telegram_id")]
    if not users:
        await call.message.answer("Нет клиентов с привязанным Telegram.")
        await call.answer()
        return

    await render_trial_add_list(call, prepare_users_for_display(users), 0, edit=False)
    await call.answer()


async def render_trial_add_list(call: CallbackQuery, users: list, page: int, edit: bool):
    page_users, total_pages, page = paginate(users, page)
    used_ids = _load_trial_used_ids()

    rows = []
    for u in page_users:
        tg_id = u["telegram_id"]
        mark = "✅ " if tg_id in used_ids else ""
        rows.append([InlineKeyboardButton(
            text=f"{mark}{u.get('username')} (id {tg_id})",
            callback_data=f"trialadd:{tg_id}"
        )])
    rows += pagination_nav_row(page, total_pages, "trialaddpage")

    suffix = f", стр. {page + 1}/{total_pages}" if total_pages > 1 else ""
    label = f"Выберите, кого пометить как уже использовавшего триал ({len(users)} всего{suffix}):"

    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    if edit:
        try:
            await call.message.edit_text(label, reply_markup=kb)
        except Exception:
            pass
    else:
        await call.message.answer(label, reply_markup=kb)


@dp.callback_query(F.data.startswith("trialaddpage:"))
async def db_trial_add_page(call: CallbackQuery):
    if not await admin_only(call):
        return

    page = int(call.data.split(":", 1)[1])
    users = [u for u in (list_users() or []) if u.get("telegram_id")]

    await render_trial_add_list(call, prepare_users_for_display(users), page, edit=True)
    await call.answer()


@dp.callback_query(F.data.startswith("trialadd:"))
async def db_trial_add_execute(call: CallbackQuery):
    if not await admin_only(call):
        return

    tg_id = int(call.data.split(":", 1)[1])
    mark_trial_used(tg_id)

    await call.message.answer(f"✅ id {tg_id} добавлен в список использовавших триал.", reply_markup=main_menu)
    await call.answer()


# ---------------- CHECK BINDINGS (silent, no message sent to clients) ----------------

async def run_binding_check() -> str:
    users = [u for u in (list_users() or []) if u.get("telegram_id")]
    if not users:
        return "Нет пользователей с привязанным Telegram."

    broken = []

    for u in users:
        try:
            # send_chat_action ("typing…") is gated by the same "bot can't
            # initiate conversation" restriction as sendMessage, but leaves
            # no message in the chat history and sends no push notification —
            # unlike get_chat(), which succeeds even without a real chat.
            await bot.send_chat_action(u["telegram_id"], "typing")
        except Exception as e:
            broken.append((u.get("username", "?"), str(e)))
        await asyncio.sleep(0.05)

    if not broken:
        return f"✅ Все {len(users)} привязок рабочие."

    MAX_SHOWN = 20
    lines = [f"• {name}: {reason}" for name, reason in broken[:MAX_SHOWN]]
    report = f"⚠️ Проблемных привязок: {len(broken)} из {len(users)}\n\n" + "\n".join(lines)

    remaining = len(broken) - MAX_SHOWN
    if remaining > 0:
        report += f"\n… и ещё {remaining}"

    report += "\n\nЭтих клиентов нужно попросить активировать бота (открыть чат и написать хоть что-нибудь)."

    return report


# ---------------- SORTING SETTINGS ----------------

def sorting_menu_kb() -> InlineKeyboardMarkup:
    grouping_on = is_grouping_enabled()
    hide_on = is_hide_unlimited_enabled()

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{'✅' if grouping_on else '⬜'} Группировка по подписке (сверху подписанные)",
            callback_data="sort:toggle_group"
        )],
        [InlineKeyboardButton(
            text=f"{'✅' if hide_on else '⬜'} Скрывать безлимитных",
            callback_data="sort:toggle_hide"
        )],
    ])


@dp.message(F.text == "⚙️ Сортировка БД")
async def sorting_menu(msg: Message):
    if not await admin_only(msg):
        return

    await msg.answer("⚙️ Настройки отображения списков (тап переключает):", reply_markup=sorting_menu_kb())


@dp.callback_query(F.data == "sort:toggle_group")
async def sorting_toggle_group(call: CallbackQuery):
    if not await admin_only(call):
        return

    toggle_grouping()
    try:
        await call.message.edit_reply_markup(reply_markup=sorting_menu_kb())
    except Exception:
        pass
    await call.answer()


@dp.callback_query(F.data == "sort:toggle_hide")
async def sorting_toggle_hide(call: CallbackQuery):
    if not await admin_only(call):
        return

    toggle_hide_unlimited()
    try:
        await call.message.edit_reply_markup(reply_markup=sorting_menu_kb())
    except Exception:
        pass
    await call.answer()


# ---------------- SYNC BUTTON ----------------

@dp.message(F.text == "🔄 Sync users")
async def sync_users(msg: Message):
    if not await admin_only(msg):
        return

    await msg.answer("🔄 Checking expirations & syncing...")

    loop = asyncio.get_event_loop()

    try:
        # Runs the same logic as the daily cleanup timer: T-7/T-3 warnings,
        # disabling anyone who has actually expired (+ notifying them and
        # the admin). If it disabled someone it already did a full resync +
        # trusttunnel restart on its own — in that case we skip the extra
        # unconditional resync below to avoid restarting the tunnel twice.
        already_resynced = await loop.run_in_executor(None, cleanup_service.run)

        if not already_resynced:
            await run_sync()

        await msg.answer("✅ Sync completed (expiry check + credentials resync)")
    except Exception as e:
        log.exception("manual sync failed")
        await msg.answer(f"❌ Sync error: {e}")


# ---------------- DEPLOY BUTTON ----------------

@dp.message(F.text == "🚀 Деплой")
async def deploy_button(msg: Message):
    if not await admin_only(msg):
        return

    await msg.answer(
        "🚀 Запускаю деплой в отдельном systemd-юните (чтобы рестарт бота его не оборвал).\n"
        "Бот сам перезапустится через несколько секунд — по завершении пришлю сообщение "
        "«✅ Деплой завершён» (или ❌, если бот не поднялся)."
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            "systemd-run",
            "--unit=trustpanel-deploy-manual",
            "--description=Manual deploy triggered from bot",
            "bash", "/opt/trustpanel/deploy.sh",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        await proc.wait()
    except OSError as e:
        log.exception("failed to launch deploy")
        await msg.answer(f"❌ Не удалось запустить деплой: {e}")


# ---------------- ADD USER FLOW ----------------

@dp.message(F.text == "➕ Add user")
async def menu_add(msg: Message):
    if not await admin_only(msg):
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Одного", callback_data="addmode:single")],
        [InlineKeyboardButton(text="👥 Несколько", callback_data="addmode:multi")],
    ])
    await msg.answer("Сколько клиентов добавляем?", reply_markup=kb)


@dp.callback_query(F.data == "addmode:single")
async def add_mode_single(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    await state.set_state(AddUser.username)
    await call.message.answer("Enter username:", reply_markup=cancel_kb)
    await call.answer()


@dp.message(AddUser.username)
async def add_username(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return
    await state.update_data(username=msg.text.strip())
    await state.set_state(AddUser.password)
    await msg.answer("Enter password:")


@dp.message(AddUser.password)
async def add_password(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return
    await state.update_data(password=msg.text.strip())
    await state.set_state(AddUser.days)

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="3 дня")],
            [KeyboardButton(text="1 мес")],
            [KeyboardButton(text="♾ Безлимит")],
            [KeyboardButton(text="📅 Ручной ввод даты")],
        ],
        resize_keyboard=True
    )

    await msg.answer("Выберите срок действия:", reply_markup=kb)


async def finalize_add_user(msg: Message, state: FSMContext, expires_at):
    data = await state.get_data()
    username = data["username"]
    password = data["password"]

    add_user({
        "username": username,
        "password": password,
        "created_at": utcnow_naive().strftime("%Y-%m-%d"),
        "expires_at": expires_at,
        "status": "active",
        "telegram_id": None,
        "notified_days": [],
        "pending_request": None,
    })

    await run_sync()

    link = generate_link(username, DOMAIN)

    await msg.answer(
        format_full_instructions_message(username, password, expires_at, link),
        reply_markup=ReplyKeyboardRemove()
    )

    await msg.answer("Menu:", reply_markup=main_menu)
    await state.clear()


def compute_new_user_expiry(label: str):
    now = utcnow_naive()

    if label == "3 дня":
        return (now + timedelta(days=3)).strftime("%Y-%m-%d")
    if label == "1 мес":
        return add_calendar_months(now, 1).strftime("%Y-%m-%d")
    if label == "♾ Безлимит":
        return None
    return None


@dp.message(AddUser.days)
async def add_days(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return

    text = msg.text.strip()

    if text == "📅 Ручной ввод даты":
        await state.set_state(AddUser.manual_date)
        await msg.answer("Введите дату истечения в формате YYYY-MM-DD:", reply_markup=cancel_kb)
        return

    if text not in ("3 дня", "1 мес", "♾ Безлимит"):
        await msg.answer("Выберите один из вариантов на клавиатуре.")
        return

    expires_at = compute_new_user_expiry(text)

    await finalize_add_user(msg, state, expires_at)


@dp.message(AddUser.manual_date)
async def add_manual_date(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return

    try:
        datetime.strptime(msg.text.strip(), "%Y-%m-%d")
    except ValueError:
        await msg.answer("Неверный формат. Введите дату как YYYY-MM-DD.")
        return

    await finalize_add_user(msg, state, msg.text.strip())


# ---------------- ADD USER FLOW (MULTIPLE) ----------------

multi_step_cancel_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="❌ Отменить этого клиента")],
        [KeyboardButton(text="❌ Отменить всё добавление")],
    ],
    resize_keyboard=True
)


@dp.callback_query(F.data == "addmode:multi")
async def add_mode_multi(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    await state.set_state(AddUserMulti.username)
    await state.update_data(batch=[])
    await call.message.answer("👤 Клиент №1 — введите username:", reply_markup=ReplyKeyboardRemove())
    await call.answer()


async def handle_multi_cancel(msg: Message, state: FSMContext) -> bool:
    """
    Handles the two special cancel buttons shared by every step of the
    multi-add loop. Returns True if the caller should stop processing
    (a cancel was handled), False if this was normal input to validate.
    """
    text = (msg.text or "").strip()

    if text == "❌ Отменить всё добавление":
        await state.clear()
        await msg.answer("Добавление отменено, никто не сохранён.", reply_markup=main_menu)
        return True

    if text == "❌ Отменить этого клиента":
        data = await state.get_data()
        batch = data.get("batch", [])

        await state.update_data(current_username=None, current_password=None)

        if not batch:
            await state.clear()
            await msg.answer("Отменил — пока никто не был добавлен в очередь.", reply_markup=main_menu)
            return True

        await msg.answer("Ок, этот клиент отменён.", reply_markup=ReplyKeyboardRemove())
        await show_multi_continue_choice(msg, state, batch)
        return True

    return False


async def show_multi_continue_choice(msg: Message, state: FSMContext, batch: list):
    await state.set_state(AddUserMulti.continue_choice)

    names = "\n".join(f"• {u['username']} ({u['expires_at'] or '∞'})" for u in batch)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить ещё", callback_data="multiadd:more")],
        [InlineKeyboardButton(text="✅ Готово", callback_data="multiadd:done")],
    ])

    await msg.answer(f"В очереди ({len(batch)}):\n{names}\n\nЧто дальше?", reply_markup=kb)


@dp.message(AddUserMulti.username)
async def multi_add_username(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return
    if await handle_multi_cancel(msg, state):
        return

    username = (msg.text or "").strip()
    data = await state.get_data()
    batch = data.get("batch", [])

    if get_user(username) or any(u["username"] == username for u in batch):
        await msg.answer("Это имя уже занято (в базе или уже в текущей очереди). Введите другое:")
        return

    await state.update_data(current_username=username)
    await state.set_state(AddUserMulti.password)
    await msg.answer("Введите password:")


@dp.message(AddUserMulti.password)
async def multi_add_password(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return
    if await handle_multi_cancel(msg, state):
        return

    password = (msg.text or "").strip()
    await state.update_data(current_password=password)
    await state.set_state(AddUserMulti.days)

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="3 дня")],
            [KeyboardButton(text="1 мес")],
            [KeyboardButton(text="♾ Безлимит")],
            [KeyboardButton(text="📅 Ручной ввод даты")],
            [KeyboardButton(text="❌ Отменить этого клиента")],
            [KeyboardButton(text="❌ Отменить всё добавление")],
        ],
        resize_keyboard=True
    )
    await msg.answer("Выберите срок действия:", reply_markup=kb)


@dp.message(AddUserMulti.days)
async def multi_add_days(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return
    if await handle_multi_cancel(msg, state):
        return

    text = (msg.text or "").strip()

    if text == "📅 Ручной ввод даты":
        await state.set_state(AddUserMulti.manual_date)
        await msg.answer("Введите дату истечения в формате YYYY-MM-DD:", reply_markup=multi_step_cancel_kb)
        return

    if text not in ("3 дня", "1 мес", "♾ Безлимит"):
        await msg.answer("Выберите один из вариантов на клавиатуре.")
        return

    await finish_multi_entry(msg, state, compute_new_user_expiry(text))


@dp.message(AddUserMulti.manual_date)
async def multi_add_manual_date(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return
    if await handle_multi_cancel(msg, state):
        return

    try:
        datetime.strptime(msg.text.strip(), "%Y-%m-%d")
    except ValueError:
        await msg.answer("Неверный формат. Введите дату как YYYY-MM-DD.")
        return

    await finish_multi_entry(msg, state, msg.text.strip())


async def finish_multi_entry(msg: Message, state: FSMContext, expires_at):
    data = await state.get_data()
    batch = data.get("batch", [])

    batch.append({
        "username": data["current_username"],
        "password": data["current_password"],
        "expires_at": expires_at,
    })

    await state.update_data(batch=batch, current_username=None, current_password=None)
    await msg.answer(f"✅ {data['current_username']} добавлен в очередь.", reply_markup=ReplyKeyboardRemove())
    await show_multi_continue_choice(msg, state, batch)


@dp.callback_query(F.data == "multiadd:more", AddUserMulti.continue_choice)
async def multi_add_more(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    data = await state.get_data()
    n = len(data.get("batch", [])) + 1

    await state.set_state(AddUserMulti.username)
    await call.message.answer(f"👤 Клиент №{n} — введите username:")
    await call.answer()


@dp.callback_query(F.data == "multiadd:done", AddUserMulti.continue_choice)
async def multi_add_done(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    data = await state.get_data()
    batch = data.get("batch", [])

    if not batch:
        await state.clear()
        await call.message.answer("Список пуст, ничего не добавлено.", reply_markup=main_menu)
        await call.answer()
        return

    created_usernames = []
    for entry in batch:
        add_user({
            "username": entry["username"],
            "password": entry["password"],
            "created_at": utcnow_naive().strftime("%Y-%m-%d"),
            "expires_at": entry["expires_at"],
            "status": "active",
            "telegram_id": None,
            "notified_days": [],
            "pending_request": None,
        })
        created_usernames.append(entry["username"])

    # One resync + trusttunnel restart for the whole batch, not per-user.
    await run_sync()

    await state.set_state(AddUserMulti.done_actions)
    await state.update_data(created_usernames=created_usernames)

    names = ", ".join(created_usernames)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Завершить", callback_data="multiadd:finish")],
        [InlineKeyboardButton(text="📇 Получить карточки", callback_data="multiadd:cards")],
    ])

    await call.message.answer(
        f"✅ Добавлено {len(created_usernames)}, туннель пересобран:\n{names}",
        reply_markup=kb
    )
    await call.answer()


@dp.callback_query(F.data == "multiadd:finish", AddUserMulti.done_actions)
async def multi_add_finish(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    await state.clear()
    await call.message.answer("Готово.", reply_markup=main_menu)
    await call.answer()


@dp.callback_query(F.data == "multiadd:cards", AddUserMulti.done_actions)
async def multi_add_get_cards(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    data = await state.get_data()
    usernames = data.get("created_usernames", [])
    await state.clear()

    for username in usernames:
        user = get_user(username)
        if not user:
            continue
        link = generate_link(username, DOMAIN)
        await call.message.answer(
            format_full_instructions_message(username, user.get("password"), user.get("expires_at"), link)
        )

    await call.message.answer("Готово — карточки отправлены выше.", reply_markup=main_menu)
    await call.answer()


# ---------------- LIST USERS ----------------

def build_list_users_kb(users: list, page: int) -> tuple:
    """Returns (keyboard, total_pages, page)."""
    page_users, total_pages, page = paginate(users, page)

    rows = [
        [InlineKeyboardButton(
            text=user_button_label(u),
            callback_data=f"user:{u.get('username')}"
        )]
        for u in page_users if u.get("username")
    ]
    rows += pagination_nav_row(page, total_pages, "listpage")

    return InlineKeyboardMarkup(inline_keyboard=rows), total_pages, page


@dp.message(F.text == "📋 List users")
async def menu_list(msg: Message):
    if not await admin_only(msg):
        return

    users = prepare_users_for_display(list_users() or [])

    if not users:
        await msg.answer("No users")
        return

    kb, total_pages, page = build_list_users_kb(users, 0)
    label = f"Select user ({len(users)} всего" + (f", стр. {page + 1}/{total_pages}" if total_pages > 1 else "") + "):"

    await msg.answer(label, reply_markup=kb)


@dp.callback_query(F.data.startswith("listpage:"))
async def menu_list_page(call: CallbackQuery):
    if not await admin_only(call):
        return

    page = int(call.data.split(":", 1)[1])
    users = prepare_users_for_display(list_users() or [])

    kb, total_pages, page = build_list_users_kb(users, page)
    label = f"Select user ({len(users)} всего" + (f", стр. {page + 1}/{total_pages}" if total_pages > 1 else "") + "):"

    try:
        await call.message.edit_text(label, reply_markup=kb)
    except Exception:
        pass  # "message is not modified" if the same page is tapped twice
    await call.answer()


# ---------------- USER ACTIONS MENU ----------------

@dp.callback_query(F.data.startswith("user:"))
async def user_actions_menu(call: CallbackQuery):
    if not await admin_only(call):
        return

    username = call.data.split(":", 1)[1]
    user = get_user(username) or {}

    tg_id = user.get("telegram_id")
    sub_status = f"🔔 Подписан (id {tg_id})" if tg_id else "🔕 Не подписан на уведомления"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Get link", callback_data=f"act_link:{username}")],
        [InlineKeyboardButton(text="⏳ Extend", callback_data=f"act_extend:{username}")],
        [InlineKeyboardButton(text="❌ Delete", callback_data=f"act_del:{username}")],
        [InlineKeyboardButton(text="✉️ Написать", callback_data=f"act_call:{username}")],
        [InlineKeyboardButton(
            text="🆔 Записать/перезаписать ID",
            callback_data=f"act_setid:{username}"
        )],
    ])

    await call.message.answer(f"👤 {username}\n{sub_status}\n\nChoose action:", reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data.startswith("act_link:"))
async def action_get_link(call: CallbackQuery):
    if not await admin_only(call):
        return

    username = call.data.split(":", 1)[1]
    user = get_user(username) or {}
    link = generate_link(username, DOMAIN)

    await call.message.answer(
        format_full_instructions_message(username, user.get("password"), user.get("expires_at"), link)
    )
    await call.answer()


@dp.callback_query(F.data.startswith("act_extend:"))
async def action_extend(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    username = call.data.split(":", 1)[1]

    await state.update_data(username=username)
    await state.set_state(ExtendUser.mode)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ 3 дня", callback_data="ext:3")],
        [InlineKeyboardButton(text="➕ 1 мес", callback_data="ext:30")],
        [InlineKeyboardButton(text="♾ Безлимит", callback_data="ext:0")],
        [InlineKeyboardButton(text="✍️ Ручной ввод", callback_data="ext:manual")]
    ])

    await call.message.answer(f"Extend user: {username}", reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data.startswith("act_del:"))
async def action_delete_confirm(call: CallbackQuery):
    if not await admin_only(call):
        return

    username = call.data.split(":", 1)[1]

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🗑 Да, удалить {username}", callback_data=f"act_del_yes:{username}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="act_del_no")],
    ])

    await call.message.answer(f"⚠️ Удалить пользователя {username}? Это необратимо.", reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data == "act_del_no")
async def action_delete_cancel(call: CallbackQuery):
    await call.message.answer("Отменено.", reply_markup=main_menu)
    await call.answer()


@dp.callback_query(F.data.startswith("act_del_yes:"))
async def action_delete_execute(call: CallbackQuery):
    if not await admin_only(call):
        return

    username = call.data.split(":", 1)[1]
    user = get_user(username)
    was_active = bool(
        user and user.get("status") == "active" and not is_expired(user.get("expires_at"))
    )

    delete_user(username)

    if was_active:
        await run_sync()

    await call.message.answer(f"❌ Deleted: {username}", reply_markup=main_menu)
    await call.answer()


@dp.callback_query(F.data.startswith("act_call:"))
async def action_call(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    username = call.data.split(":", 1)[1]
    user = get_user(username)

    if not user or not user.get("telegram_id"):
        await call.answer("У пользователя нет привязанного Telegram", show_alert=True)
        return

    await state.update_data(target_username=username)
    await state.set_state(AdminMessage.personal)

    await call.message.answer(f"Введите сообщение для {username}:", reply_markup=cancel_kb)
    await call.answer()


@dp.callback_query(F.data.startswith("act_setid:"))
async def action_set_id_start(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    username = call.data.split(":", 1)[1]

    await state.set_state(SetTelegramId.waiting)
    await state.update_data(target_username=username)

    await call.message.answer(
        f"Отправьте Telegram ID клиента {username} числом,\n"
        f"или перешлите сюда любое его сообщение — возьму ID оттуда.",
        reply_markup=cancel_kb
    )
    await call.answer()


@dp.message(SetTelegramId.waiting)
async def action_set_id_apply(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return

    data = await state.get_data()
    username = data.get("target_username")
    await state.clear()

    tg_id = extract_telegram_id_from_message(msg)

    if tg_id is None:
        await msg.answer(
            "Не смог распознать ID. Пришлите число, либо перешлите сообщение от клиента "
            "(не сработает, если у него в приватности скрыта пересылка).",
            reply_markup=main_menu
        )
        return

    update_user(username, telegram_id=tg_id)

    await msg.answer(
        f"✅ {username} теперь привязан к Telegram ID {tg_id}. Уведомления будут приходить туда.",
        reply_markup=main_menu
    )


# ---------------- EXTEND ----------------

@dp.callback_query(F.data.startswith("ext:"))
async def extend_handler(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    data = await state.get_data()
    username = data.get("username")

    mode = call.data.split(":")[1]

    user = get_user(username)
    if not user:
        await call.message.answer("User not found")
        return

    was_expired_or_inactive = user.get("status") != "active" or is_expired(user.get("expires_at"))
    new_expires_at = user.get("expires_at")

    if mode == "0":
        update_user(username, expires_at=None, status="active", notified_days=[])
        new_expires_at = None

    elif mode == "3":
        new_expires_at = calc_new_expiry(user.get("expires_at"), 3)
        update_user(username, expires_at=new_expires_at, status="active", notified_days=[])

    elif mode == "30":
        # "1 месяц" must be a calendar month (same day next month), not a
        # flat +30 days — otherwise short months quietly shift the renewal
        # date earlier every cycle.
        new_expires_at = calc_new_expiry_months(user.get("expires_at"), 1)
        update_user(username, expires_at=new_expires_at, status="active", notified_days=[])

    elif mode == "manual":
        await state.set_state(ExtendUser.manual)
        await call.message.answer("Send date YYYY-MM-DD")
        await call.answer()
        return

    if was_expired_or_inactive:
        await run_sync()

    if user.get("telegram_id"):
        expiry_line = "бессрочно" if not new_expires_at else new_expires_at
        await bot.send_message(
            user["telegram_id"],
            f"✅ Ваша подписка продлена. Доступ действует до: {expiry_line}"
        )

    await state.clear()

    expiry_label = "бессрочно" if not new_expires_at else new_expires_at
    await call.message.answer(f"✅ {username}: обновлено, доступ до {expiry_label}")
    await call.answer()


@dp.message(ExtendUser.manual)
async def manual_date(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return

    data = await state.get_data()
    username = data.get("username")

    try:
        datetime.strptime(msg.text.strip(), "%Y-%m-%d")
    except ValueError:
        # BUGFIX: was a bare `except:`, which also swallows KeyboardInterrupt
        # / SystemExit / genuine bugs elsewhere in this block, not just a
        # bad date string.
        await msg.answer("Wrong format YYYY-MM-DD")
        return

    user = get_user(username) or {}
    was_expired_or_inactive = user.get("status") != "active" or is_expired(user.get("expires_at"))
    new_expires_at = msg.text.strip()

    update_user(username, expires_at=new_expires_at, status="active", notified_days=[])

    if was_expired_or_inactive:
        await run_sync()

    if user.get("telegram_id"):
        await bot.send_message(
            user["telegram_id"],
            f"✅ Ваша подписка продлена. Доступ действует до: {new_expires_at}"
        )

    await state.clear()
    await msg.answer(f"✅ {username}: обновлено, доступ до {new_expires_at}")


# ---------------- MASS DELETE (checkbox picker, same UX as selective broadcast) ----------------

def build_delete_kb(users: list, selected: set, page: int) -> tuple:
    page_users, total_pages, page = paginate(users, page)

    rows = [
        [InlineKeyboardButton(
            text=("☑️ " if u.get("username") in selected else "⬜ ") + user_button_label(u),
            callback_data=f"deltoggle:{u['username']}"
        )]
        for u in page_users if u.get("username")
    ]
    rows += pagination_nav_row(page, total_pages, "delpage")
    rows.append([
        InlineKeyboardButton(text=f"🗑 Готово ({len(selected)})", callback_data="delsel:done"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="delsel:cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows), total_pages, page


def mass_delete_label(total: int, page: int, total_pages: int) -> str:
    suffix = f", стр. {page + 1}/{total_pages}" if total_pages > 1 else ""
    return f"Выберите пользователей для удаления ({total} всего{suffix}), тап переключает ☑️/⬜:"


@dp.message(F.text == "🗑 Удаление пользователей")
async def mass_delete_start(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return

    users = prepare_users_for_display([u for u in (list_users() or []) if u.get("username")])
    if not users:
        await msg.answer("Список пользователей пуст.")
        return

    await state.set_state(MassDelete.select)
    await state.update_data(selected=[], page=0)

    kb, total_pages, page = build_delete_kb(users, set(), 0)
    await msg.answer(mass_delete_label(len(users), page, total_pages), reply_markup=kb)


@dp.callback_query(F.data.startswith("delpage:"), MassDelete.select)
async def mass_delete_page(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    page = int(call.data.split(":", 1)[1])
    await state.update_data(page=page)

    data = await state.get_data()
    selected = set(data.get("selected", []))
    users = prepare_users_for_display([u for u in (list_users() or []) if u.get("username")])

    kb, total_pages, page = build_delete_kb(users, selected, page)
    try:
        await call.message.edit_text(mass_delete_label(len(users), page, total_pages), reply_markup=kb)
    except Exception:
        pass
    await call.answer()


@dp.callback_query(F.data.startswith("deltoggle:"), MassDelete.select)
async def toggle_delete_selection(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    try:
        username = call.data.split(":", 1)[1]

        data = await state.get_data()
        selected = set(data.get("selected", []))
        page = data.get("page", 0)

        if username in selected:
            selected.discard(username)
        else:
            selected.add(username)

        await state.update_data(selected=list(selected))

        users = prepare_users_for_display([u for u in (list_users() or []) if u.get("username")])

        try:
            kb, total_pages, page = build_delete_kb(users, selected, page)
            await call.message.edit_reply_markup(reply_markup=kb)
        except Exception as e:
            log.debug("edit_reply_markup failed (likely unchanged markup): %s", e)

        await call.answer()
    except Exception as e:
        log.exception("mass delete toggle failed")
        await call.answer(f"Ошибка: {e}", show_alert=True)


@dp.callback_query(F.data == "delsel:cancel", MassDelete.select)
async def cancel_mass_delete(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    await state.clear()
    await call.message.answer("Отменено.", reply_markup=main_menu)
    await call.answer()


@dp.callback_query(F.data == "delsel:done", MassDelete.select)
async def mass_delete_ask_confirmation(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    try:
        data = await state.get_data()
        selected = data.get("selected", [])

        if not selected:
            await call.answer("Выберите хотя бы одного пользователя", show_alert=True)
            return

        await state.set_state(MassDelete.confirm)

        names = "\n".join(f"• {u}" for u in selected)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🗑 Да, удалить ({len(selected)})", callback_data="delconfirm:yes")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="delconfirm:no")],
        ])

        await call.message.answer(
            f"⚠️ Удалить {len(selected)} пользователей? Это необратимо:\n\n{names}",
            reply_markup=kb
        )
        await call.answer()
    except Exception as e:
        log.exception("mass delete confirmation step failed")
        await call.answer(f"Ошибка: {e}", show_alert=True)


@dp.callback_query(F.data.startswith("delconfirm:"), MassDelete.confirm)
async def mass_delete_execute(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    action = call.data.split(":")[1]

    data = await state.get_data()
    selected = data.get("selected", [])
    await state.clear()

    if action == "no":
        await call.message.answer("Отменено.", reply_markup=main_menu)
        await call.answer()
        return

    deleted = 0
    needs_resync = False

    for username in selected:
        user = get_user(username)

        # Was this user actually present in credentials.toml before deletion?
        # rebuild_credentials_from_db() only includes active, non-expired users —
        # deleting someone who was already inactive/expired changes nothing there.
        if user and user.get("status") == "active" and not is_expired(user.get("expires_at")):
            needs_resync = True

        try:
            delete_user(username)
            deleted += 1
        except Exception as e:
            log.exception("mass delete failed for %s", username)

    # One resync + trusttunnel restart for the whole batch, not per-user — and
    # only if it's actually needed, to avoid disrupting every connected client
    # for a no-op (e.g. cleaning up a batch of already-expired accounts).
    if needs_resync:
        await run_sync()

    await call.message.answer(f"✅ Удалено пользователей: {deleted}", reply_markup=main_menu)
    await call.answer()


# ---------------- GET LINK ----------------

def build_get_link_kb(users: list, page: int) -> tuple:
    page_users, total_pages, page = paginate(users, page)

    rows = [
        [InlineKeyboardButton(
            text=user_button_label(u),
            callback_data=f"link:{u.get('username')}"
        )]
        for u in page_users if u.get("username")
    ]
    rows += pagination_nav_row(page, total_pages, "linkpage")

    return InlineKeyboardMarkup(inline_keyboard=rows), total_pages, page


@dp.message(F.text == "🔗 Get link")
async def menu_link(msg: Message):
    if not await admin_only(msg):
        return

    users = prepare_users_for_display(list_users() or [])

    if not users:
        await msg.answer("No users")
        return

    kb, total_pages, page = build_get_link_kb(users, 0)
    label = f"Select user ({len(users)} всего" + (f", стр. {page + 1}/{total_pages}" if total_pages > 1 else "") + "):"

    await msg.answer(label, reply_markup=kb)


@dp.callback_query(F.data.startswith("linkpage:"))
async def menu_link_page(call: CallbackQuery):
    if not await admin_only(call):
        return

    page = int(call.data.split(":", 1)[1])
    users = prepare_users_for_display(list_users() or [])

    kb, total_pages, page = build_get_link_kb(users, page)
    label = f"Select user ({len(users)} всего" + (f", стр. {page + 1}/{total_pages}" if total_pages > 1 else "") + "):"

    try:
        await call.message.edit_text(label, reply_markup=kb)
    except Exception:
        pass
    await call.answer()


@dp.callback_query(F.data.startswith("link:"))
async def link_callback(call: CallbackQuery):
    if not await admin_only(call):
        return

    username = call.data.split(":")[1]

    user = get_user(username) or {}
    link = generate_link(username, DOMAIN)

    await call.message.answer(
        format_full_instructions_message(username, user.get("password"), user.get("expires_at"), link)
    )

    await call.answer()


# ---------------- MAIN ----------------

async def main():
    log.info("Starting bot...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
