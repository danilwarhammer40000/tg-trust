"""
Owns: DBImport.waiting (defined in bot/states.py).
"""
import io
import json
import logging
import os
import shutil
import zipfile

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.access import admin_only, run_sync
from bot.config import bot
from bot.display import prepare_users_for_display
from bot.keyboards import cancel_kb, main_menu
from bot.pagination import paginate, pagination_nav_row
from bot.states import DBImport
from bot.trial import load_trial_used_ids, save_trial_used_ids, mark_trial_used
from core.dates import utcnow_naive
from core.db import get_user_by_telegram_id, list_users
from core.paths import BACKUP_FILES

router = Router()
log = logging.getLogger(__name__)


def build_db_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, path in BACKUP_FILES.items():
            if os.path.exists(path):
                zf.write(path, arcname=arcname)
    buf.seek(0)
    return buf.read()


@router.message(F.text == "🗄 База данных")
async def db_menu(msg: Message):
    if not await admin_only(msg):
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Выгрузить сейчас", callback_data="db:export")],
        [InlineKeyboardButton(text="📥 Загрузить БД", callback_data="db:import")],
        [InlineKeyboardButton(text="🎟 Управление триалами", callback_data="db:trials")],
    ])
    await msg.answer("🗄 База данных:", reply_markup=kb)


@router.callback_query(F.data == "db:export")
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


@router.callback_query(F.data == "db:import")
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
    A backup restore that only checked json.loads() succeeds would accept
    *any* valid JSON (e.g. `{}` or `"hello"`) and would silently wipe out
    users.json with garbage. This checks the actual expected shape: a list
    of dicts each with at least a username.
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return False

    if not isinstance(data, list):
        return False

    return all(isinstance(item, dict) and item.get("username") for item in data)


@router.message(DBImport.waiting, F.document)
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


# ---------------- TRIAL MANAGEMENT ----------------

def trial_used_label(tg_id: int) -> str:
    user = get_user_by_telegram_id(tg_id)
    if user and user.get("username"):
        return f"{user['username']} (id {tg_id})"
    return f"id {tg_id} (аккаунт не найден в БД)"


@router.callback_query(F.data == "db:trials")
async def db_trials_menu(call: CallbackQuery):
    if not await admin_only(call):
        return

    count = len(load_trial_used_ids())

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить (заблокировать триал)", callback_data="db:trial_add_list")],
        [InlineKeyboardButton(text="➖ Убрать (разрешить новый триал)", callback_data="db:trial_remove_list")],
    ])
    await call.message.answer(f"🎟 Использовано триалов: {count}", reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "db:trial_remove_list")
async def db_trial_remove_list(call: CallbackQuery):
    if not await admin_only(call):
        return

    ids = sorted(load_trial_used_ids())
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


@router.callback_query(F.data.startswith("trialdelpage:"))
async def db_trial_remove_page(call: CallbackQuery):
    if not await admin_only(call):
        return

    page = int(call.data.split(":", 1)[1])
    ids = sorted(load_trial_used_ids())

    await render_trial_remove_list(call, ids, page, edit=True)
    await call.answer()


@router.callback_query(F.data.startswith("trialdel:"))
async def db_trial_remove_execute(call: CallbackQuery):
    if not await admin_only(call):
        return

    tg_id = int(call.data.split(":", 1)[1])
    ids = load_trial_used_ids()

    if tg_id in ids:
        ids.discard(tg_id)
        save_trial_used_ids(ids)
        await call.message.answer(f"✅ id {tg_id} удалён из списка — сможет получить новый триал.", reply_markup=main_menu)
    else:
        await call.message.answer("Уже не в списке.", reply_markup=main_menu)

    await call.answer()


@router.callback_query(F.data == "db:trial_add_list")
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
    used_ids = load_trial_used_ids()

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


@router.callback_query(F.data.startswith("trialaddpage:"))
async def db_trial_add_page(call: CallbackQuery):
    if not await admin_only(call):
        return

    page = int(call.data.split(":", 1)[1])
    users = [u for u in (list_users() or []) if u.get("telegram_id")]

    await render_trial_add_list(call, prepare_users_for_display(users), page, edit=True)
    await call.answer()


@router.callback_query(F.data.startswith("trialadd:"))
async def db_trial_add_execute(call: CallbackQuery):
    if not await admin_only(call):
        return

    tg_id = int(call.data.split(":", 1)[1])
    mark_trial_used(tg_id)

    await call.message.answer(f"✅ id {tg_id} добавлен в список использовавших триал.", reply_markup=main_menu)
    await call.answer()
