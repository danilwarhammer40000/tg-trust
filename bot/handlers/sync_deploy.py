import asyncio
import logging

from aiogram import Router, F
from aiogram.types import Message

from bot.access import admin_only, run_sync
from services import cleanup as cleanup_service

router = Router()
log = logging.getLogger(__name__)


@router.message(F.text == "🔄 Sync users")
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


@router.message(F.text == "🚀 Деплой")
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
