import os

from core.db import DB_PATH

DATA_DIR = os.path.dirname(DB_PATH)

TRIAL_USED_PATH = os.path.join(DATA_DIR, "trial_used.json")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
AUTO_RENEWAL_SETTINGS_PATH = os.path.join(DATA_DIR, "auto_renewal_settings.json")
# Xray lives in its own file (server key + per-user clients), and a snapshot of
# the TrustTunnel config rides along: both must survive a server reinstall.
XRAY_STORE_PATH = os.path.join(DATA_DIR, "xray.json")
STACK_SNAPSHOT_PATH = os.path.join(DATA_DIR, "tt_config.json")
MESSAGES_PATH = os.path.join(DATA_DIR, "messages.json")

# All files that make up "the database" for backup/restore purposes.
# Deliberately does NOT include credentials.toml — that's derived data,
# rebuilt from users.json by core.service.full_resync_and_reload().
BACKUP_FILES = {
    "users.json": DB_PATH,
    "xray.json": XRAY_STORE_PATH,
    "tt_config.json": STACK_SNAPSHOT_PATH,
    "trial_used.json": TRIAL_USED_PATH,
    "settings.json": SETTINGS_PATH,
    "auto_renewal_settings.json": AUTO_RENEWAL_SETTINGS_PATH,
    "messages.json": MESSAGES_PATH,
}
