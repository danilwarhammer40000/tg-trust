import os

from core.db import DB_PATH

DATA_DIR = os.path.dirname(DB_PATH)

TRIAL_USED_PATH = os.path.join(DATA_DIR, "trial_used.json")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
AUTO_RENEWAL_SETTINGS_PATH = os.path.join(DATA_DIR, "auto_renewal_settings.json")

# All files that make up "the database" for backup/restore purposes.
# Deliberately does NOT include credentials.toml — that's derived data,
# rebuilt from users.json by core.service.full_resync_and_reload().
BACKUP_FILES = {
    "users.json": DB_PATH,
    "trial_used.json": TRIAL_USED_PATH,
    "settings.json": SETTINGS_PATH,
    "auto_renewal_settings.json": AUTO_RENEWAL_SETTINGS_PATH,
}
