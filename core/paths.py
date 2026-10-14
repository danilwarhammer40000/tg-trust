import os

from core.db import DB_PATH

DATA_DIR = os.path.dirname(DB_PATH)

TRIAL_USED_PATH = os.path.join(DATA_DIR, "trial_used.json")
TRIAL_REQUESTS_PATH = os.path.join(DATA_DIR, "trial_requests.json")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
AUTO_RENEWAL_SETTINGS_PATH = os.path.join(DATA_DIR, "auto_renewal_settings.json")
MESSAGES_PATH = os.path.join(DATA_DIR, "messages.json")
RECEIPT_HASHES_PATH = os.path.join(DATA_DIR, "receipt_hashes.json")

# All files that make up "the database" for backup/restore purposes.
# Deliberately does NOT include credentials.toml — that's derived data,
# rebuilt from users.json by core.service.full_resync_and_reload().
BACKUP_FILES = {
    "users.json": DB_PATH,
    "trial_used.json": TRIAL_USED_PATH,
    "trial_requests.json": TRIAL_REQUESTS_PATH,
    "settings.json": SETTINGS_PATH,
    "auto_renewal_settings.json": AUTO_RENEWAL_SETTINGS_PATH,
    "messages.json": MESSAGES_PATH,
    "receipt_hashes.json": RECEIPT_HASHES_PATH,
}
