"""
Optional Xray (VLESS + XHTTP) backend for TrustPanel, admin-only.

ALL Xray data lives in its own file, data/xray.json, separate from users.json:

    {
      "version": 1,
      "server":  { decryption, encryption, domain, edge_addr, port, path,
                   padding_key, padding_placement },
      "clients": { "<username>": {"uuid": "...", "enabled": true}, ... }
    }

  * "server"  is the vlessenc key pair plus the public link parameters. It is
    part of the bot's backup: after a reinstall + restore, every old link keeps
    working (same key, same UUIDs).
  * "clients" holds who has Xray. Access is OPT-IN: nobody gets it until the
    admin switches it on for that user in the bot. Links are never shown to
    clients, only in admin handlers.

users.json still decides WHO IS ALLOWED AT ALL (status + expiry): a client in
xray.json only reaches Xray while their users.json record is active and not
expired, so one subscription term governs both VPNs.

On every full resync (core.service.full_resync_and_reload) this module makes
the Xray config match that state:
  * clients = enabled xray.json clients whose user is active and not expired
              (managed ones carry email "tp:<name>"; clients added to the
              Xray config by hand are preserved),
  * inbound = decryption key / path / padding from xray.json["server"].

Safety rules:
  * Disabled (no-op) unless xray.json has a server section (or XRAY_ENC and
    XRAY_DOMAIN are set in the environment).
  * A corrupt xray.json disables syncing (the live config is left alone) and
    makes every mutation refuse, so it can never be silently overwritten.
  * The new config is validated with `xray run -test` BEFORE it replaces the
    live one, and a backup is restored if the service fails to come back.
  * Xray is restarted only if the config changed (a restart drops live
    connections) or if the service is found not running.
  * Fail closed: an unparseable expiry date means no access.
"""
import html
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

from filelock import FileLock

from core.dates import is_expired, parse_expiry, utcnow_naive
from core.db import list_users
from core.paths import XRAY_STORE_PATH

log = logging.getLogger(__name__)

EMAIL_PREFIX = "tp:"
STORE_VERSION = 1

DEFAULTS = {
    "port": 443,
    "path": "/api/v1/sync",
    "padding_key": "pref",
    "padding_placement": "cookie",
}


class StoreError(RuntimeError):
    """xray.json exists but cannot be read."""


# ---------------- the store (xray.json) ----------------

def empty_store() -> Dict:
    return {"version": STORE_VERSION, "server": {}, "clients": {}}


def _normalise(data) -> Dict:
    if not isinstance(data, dict):
        raise StoreError("xray.json is not a JSON object")
    store = empty_store()
    if isinstance(data.get("server"), dict):
        store["server"] = data["server"]
    if isinstance(data.get("clients"), dict):
        store["clients"] = {
            str(name): c for name, c in data["clients"].items() if isinstance(c, dict)
        }
    return store


def load_store(strict: bool = False) -> Dict:
    """
    Missing file -> empty store. Unreadable/corrupt file -> StoreError if
    strict, else an empty store (callers that only READ treat that as
    "Xray not configured").
    """
    try:
        with open(XRAY_STORE_PATH, "r") as f:
            return _normalise(json.load(f))
    except FileNotFoundError:
        return empty_store()
    except (OSError, json.JSONDecodeError, StoreError) as e:
        if strict:
            raise StoreError(f"cannot read {XRAY_STORE_PATH}: {e}") from e
        log.error("cannot read %s: %s", XRAY_STORE_PATH, e)
        return empty_store()


def save_store(store: Dict) -> None:
    directory = os.path.dirname(XRAY_STORE_PATH)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory)
    with os.fdopen(fd, "w") as f:
        json.dump(store, f, indent=2)
        f.write("\n")
    os.chmod(tmp, 0o600)  # holds the server's private key
    os.replace(tmp, XRAY_STORE_PATH)


def _lock() -> FileLock:
    return FileLock(XRAY_STORE_PATH + ".lock", timeout=30)


# ---------------- settings ----------------

def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def config_path() -> str:
    return _env("XRAY_CONFIG_PATH", "/usr/local/etc/xray/config.json")


def settings(store: Optional[Dict] = None) -> Dict:
    """Server section of xray.json, overridable by environment variables."""
    store = store if store is not None else load_store()
    s = dict(DEFAULTS)
    s.update({k: v for k, v in store["server"].items() if v not in (None, "")})

    for key, env in (
        ("domain", "XRAY_DOMAIN"),
        ("edge_addr", "XRAY_EDGE_ADDR"),
        ("encryption", "XRAY_ENC"),
        ("path", "XRAY_PATH"),
        ("port", "XRAY_PORT"),
    ):
        val = _env(env)
        if val:
            s[key] = val

    s.setdefault("edge_addr", s.get("domain", ""))
    return s


def is_enabled() -> bool:
    s = settings()
    return bool(s.get("encryption") and s.get("domain"))


# ---------------- pure helpers (unit-tested) ----------------

def managed_email(username: str) -> str:
    return EMAIL_PREFIX + username


def compute_desired(users: List[Dict], clients: Dict[str, Dict], today=None) -> Dict[str, str]:
    """username -> uuid for everyone who should currently reach Xray."""
    if today is None:
        today = utcnow_naive().date()

    desired: Dict[str, str] = {}

    for u in users:
        if u.get("status") != "active":
            continue

        username = u.get("username")
        client = clients.get(username) if username else None
        if not client or client.get("enabled") is not True or not client.get("uuid"):
            continue

        exp = u.get("expires_at")
        if exp:
            exp_dt = parse_expiry(exp)
            if exp_dt is None or exp_dt.date() < today:
                continue  # fail closed

        desired[username] = client["uuid"]

    return desired


def adopt_legacy(users: List[Dict], clients: Dict[str, Dict]) -> bool:
    """
    One-time migration for installs that stored xray_uuid inside users.json
    (the first version of this feature): copies them into xray.json.
    Returns True if `clients` changed.
    """
    changed = False
    for u in users:
        name, legacy = u.get("username"), u.get("xray_uuid")
        if not name or not legacy or name in clients:
            continue
        clients[name] = {"uuid": legacy, "enabled": u.get("xray_enabled") is not False}
        changed = True
    return changed


def _vless_inbound(cfg: Dict) -> Dict:
    inbound = next(
        (i for i in cfg.get("inbounds", []) if i.get("protocol") == "vless"),
        None,
    )
    if inbound is None:
        raise RuntimeError("no vless inbound found in the Xray config")
    return inbound


def merge_clients(config: Dict, desired: Dict[str, str]) -> Tuple[Dict, bool]:
    """
    Returns (new_config, changed). Keeps unmanaged clients, replaces the
    managed ("tp:" prefixed) ones with `desired`. Does not mutate `config`.
    """
    new_cfg = json.loads(json.dumps(config))

    inbound = _vless_inbound(new_cfg)
    inbound_settings = inbound.setdefault("settings", {})
    clients = inbound_settings.setdefault("clients", [])

    def is_managed(c: Dict) -> bool:
        return str(c.get("email", "")).startswith(EMAIL_PREFIX)

    old_managed = {(c.get("email"), c.get("id")) for c in clients if is_managed(c)}
    keep = [c for c in clients if not is_managed(c)]
    managed = [
        {"id": desired[name], "email": managed_email(name)}
        for name in sorted(desired)
    ]
    new_managed = {(c["email"], c["id"]) for c in managed}

    inbound_settings["clients"] = keep + managed
    return new_cfg, old_managed != new_managed


def apply_identity(config: Dict, ident: Dict) -> Tuple[Dict, bool]:
    """
    Makes the VLESS inbound match xray.json["server"]: decryption key, XHTTP
    path and padding. This is what makes "reinstall + restore the DB" bring
    the old server key back. Does not mutate `config`. No-op if the identity
    has no decryption (legacy installs configured through the environment).
    """
    new_cfg = json.loads(json.dumps(config))
    if not ident.get("decryption"):
        return new_cfg, False

    inbound = _vless_inbound(new_cfg)
    s = {**DEFAULTS, **ident}

    wanted_xhttp = {
        "path": s["path"],
        "mode": "packet-up",
        "xPaddingObfsMode": True,
        "xPaddingKey": s["padding_key"],
        "xPaddingPlacement": s["padding_placement"],
    }

    before = json.dumps(inbound, sort_keys=True)

    inbound.setdefault("settings", {})["decryption"] = ident["decryption"]
    stream = inbound.setdefault("streamSettings", {})
    stream.setdefault("network", "xhttp")
    stream.setdefault("security", "none")
    stream.setdefault("xhttpSettings", {}).update(wanted_xhttp)

    return new_cfg, json.dumps(inbound, sort_keys=True) != before


def build_link(username: str, xray_uuid: str, store: Optional[Dict] = None) -> str:
    s = settings(store)
    domain = s["domain"]
    extra = json.dumps(
        {
            "xPaddingObfsMode": True,
            "xPaddingKey": s["padding_key"],
            "xPaddingPlacement": s["padding_placement"],
        },
        separators=(",", ":"),
    )

    return (
        f"vless://{xray_uuid}@{s['edge_addr']}:{s['port']}"
        f"?encryption={s['encryption']}"
        f"&security=tls&sni={domain}&fp=chrome&alpn=h2"
        f"&type=xhttp&host={domain}&path={quote(str(s['path']), safe='')}&mode=packet-up"
        f"&extra={quote(extra, safe='')}"
        f"#{quote(username, safe='')}"
    )


def client_state(user: Optional[Dict], store: Optional[Dict] = None) -> Dict:
    """What the admin card needs: {configured, enabled, uuid, reachable, reason}."""
    store = store if store is not None else load_store()
    if not is_enabled():
        return {"configured": False, "enabled": False, "uuid": None, "reachable": False,
                "reason": "Xray не настроен на сервере"}

    user = user or {}
    client = store["clients"].get(user.get("username") or "") or {}
    enabled = client.get("enabled") is True and bool(client.get("uuid"))

    reason = ""
    if enabled:
        if user.get("status") != "active":
            reason = "пользователь отключён"
        elif is_expired(user.get("expires_at")):
            reason = "срок доступа истёк"

    return {"configured": True, "enabled": enabled, "uuid": client.get("uuid"),
            "reachable": enabled and not reason, "reason": reason}


def xray_block(user: Optional[Dict]) -> str:
    """
    HTML snippet with the user's Xray link, or "" when there is nothing to
    show. ADMIN HANDLERS ONLY: it is deliberately not used in any client
    menu. Send it with parse_mode="HTML".
    """
    if not user:
        return ""
    store = load_store()
    state = client_state(user, store)
    if not state["enabled"]:
        return ""

    link = build_link(user["username"], state["uuid"], store)
    note = f"\n⚠️ Сейчас не работает: {state['reason']}." if state["reason"] else ""
    return (
        "🛰 <b>Xray (VLESS)</b>\n"
        f"<code>{html.escape(link)}</code>{note}\n\n"
        "Нажмите на ссылку, чтобы скопировать, и импортируйте её из буфера обмена "
        "в Xray-клиент (v2RayTun, v2rayNG и т. п.)."
    )


# ---------------- mutations (admin actions) ----------------

def _mutate(username: str, fn) -> Dict:
    if not username:
        raise ValueError("empty username")
    with _lock():
        store = load_store(strict=True)
        result = fn(store["clients"])
        save_store(store)
        return result


def enable_client(username: str) -> str:
    """Switches Xray on for `username` (keeps the old UUID if there was one). Returns the UUID."""
    def go(clients):
        c = clients.get(username)
        if not c or not c.get("uuid"):
            c = {"uuid": str(uuid.uuid4())}
        c["enabled"] = True
        clients[username] = c
        return {"uuid": c["uuid"]}
    return _mutate(username, go)["uuid"]


def disable_client(username: str) -> None:
    """Switches Xray off; the UUID is kept so re-enabling restores the same link."""
    def go(clients):
        if username in clients:
            clients[username]["enabled"] = False
        return {}
    _mutate(username, go)


def rotate_client(username: str) -> str:
    """Issues a NEW UUID (the old link stops working). Returns it."""
    def go(clients):
        c = clients.get(username) or {}
        c["uuid"] = str(uuid.uuid4())
        c.setdefault("enabled", True)
        clients[username] = c
        return {"uuid": c["uuid"]}
    return _mutate(username, go)["uuid"]


# ---------------- config + service ----------------

def _read_config() -> Dict:
    with open(config_path(), "r") as f:
        return json.load(f)


def _validate(path: str) -> None:
    binary = _env("XRAY_BIN", "/usr/local/bin/xray")
    result = subprocess.run(
        [binary, "run", "-test", "-config", path],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        tail = (result.stdout + result.stderr).strip()[-500:]
        raise RuntimeError(f"xray config test failed: {tail}")


def _write_config(cfg: Dict) -> str:
    """Validates, backs up the live config, swaps in the new one. Returns backup path."""
    path = config_path()
    directory = os.path.dirname(path)

    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=2)
        # Xray runs as "nobody": a 0600 root-owned file makes it fail to start.
        os.chmod(tmp, 0o644)
        _validate(tmp)

        backup = path + ".bak"
        shutil.copy2(path, backup)
        os.replace(tmp, path)
        return backup
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _service() -> str:
    return _env("XRAY_SERVICE", "xray.service")


def _service_state() -> str:
    return subprocess.run(
        ["systemctl", "is-active", _service()], capture_output=True, text=True
    ).stdout.strip()


def _restart_and_check() -> None:
    subprocess.run(["systemctl", "restart", _service()], capture_output=True, text=True)
    time.sleep(1.5)
    state = _service_state()
    if state != "active":
        raise RuntimeError(f"xray service is '{state}' after restart")


def sync_xray_from_users() -> bool:
    """
    Makes the Xray config match users.json + xray.json.
    Returns True if Xray was (re)started. Raises on failure after rolling
    back to the previous config.
    """
    with _lock():
        store = load_store()
        if not is_enabled():
            return False

        users = list_users()
        if adopt_legacy(users, store["clients"]):
            save_store(store)

        desired = compute_desired(users, store["clients"])

        cfg = _read_config()
        new_cfg, clients_changed = merge_clients(cfg, desired)
        new_cfg, identity_changed = apply_identity(new_cfg, store["server"])

        if not (clients_changed or identity_changed):
            if _service_state() != "active":
                log.warning("xray config is current but the service is not active, starting it")
                _restart_and_check()
                return True
            log.info("xray: unchanged (%d clients), no restart", len(desired))
            return False

        backup = _write_config(new_cfg)
        try:
            _restart_and_check()
        except Exception:
            log.exception("xray restart failed, rolling back to %s", backup)
            shutil.copy2(backup, config_path())
            os.chmod(config_path(), 0o644)
            subprocess.run(["systemctl", "restart", _service()], capture_output=True)
            raise

        log.info("xray: updated (%d clients, identity_changed=%s), restarted",
                 len(desired), identity_changed)
        return True
