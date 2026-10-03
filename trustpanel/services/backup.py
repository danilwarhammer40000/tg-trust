import logging
import os
import sys
import zipfile
import io
from datetime import datetime, timezone

sys.path.append("/opt/trustpanel")

from core.logging_setup import setup_logging
from core.paths import BACKUP_FILES
from core.notify import send_document, notify_admin

setup_logging()
log = logging.getLogger(__name__)


def build_backup_zip() -> bytes:
    """
    Packages users.json + trial_used.json + settings.json into one zip.
    credentials.toml is deliberately NOT included — it's derived data,
    rebuilt from users.json by core.service.full_resync_and_reload().
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, path in BACKUP_FILES.items():
            if os.path.exists(path):
                zf.write(path, arcname=arcname)
    buf.seek(0)
    return buf.read()


def run():
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    filename = f"trustpanel_backup_{ts}.zip"

    data = build_backup_zip()

    tmp_path = f"/tmp/{filename}"
    with open(tmp_path, "wb") as f:
        f.write(data)
    os.chmod(tmp_path, 0o600)  # backup contains plaintext passwords

    try:
        ok = send_document(
            tmp_path,
            filename=filename,
            caption=f"🗄 Плановый бэкап БД ({ts} UTC)",
        )
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    if ok:
        log.info("Backup sent: %s", filename)
    else:
        log.error("Failed to send backup %s", filename)
        notify_admin(
            f"⚠️ Плановый бэкап {filename} не удалось отправить — "
            f"проверьте journalctl -u trustpanel-backup"
        )


if __name__ == "__main__":
    run()
