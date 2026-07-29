import logging
import os
import re
import subprocess

log = logging.getLogger(__name__)

TRUSTTUNNEL_DIR = os.getenv("TRUSTTUNNEL_DIR", "/opt/trusttunnel")


def resolve_endpoint_binary():
    env_path = os.getenv("TRUSTTUNNEL_ENDPOINT_BIN")

    if env_path:
        env_path = os.path.abspath(env_path)
        if os.path.isfile(env_path):
            return env_path

    server_path = os.path.join(TRUSTTUNNEL_DIR, "trusttunnel_endpoint")

    if os.path.isfile(server_path):
        return server_path

    return None


def validate_domain(domain: str) -> None:
    if not domain:
        raise ValueError("Domain is empty")

    domain = domain.strip()

    if any(x in domain for x in [" ", ";", "&", "|", "$", "`"]):
        raise ValueError("Invalid domain")

    if domain.startswith("http://") or domain.startswith("https://"):
        raise ValueError("Domain must not include scheme")

    if not re.match(r"^[a-zA-Z0-9.-]+$", domain):
        raise ValueError("Invalid domain format")


def generate_link(username: str, domain: str) -> str:
    validate_domain(domain)

    binary_path = resolve_endpoint_binary()

    fallback_url = f"https://{domain}/connect/{username}"

    if not binary_path:
        return fallback_url

    cmd = [binary_path, "vpn.toml", "hosts.toml", "-c", username, "-a", domain]

    try:
        result = subprocess.run(
            cmd,
            cwd=os.path.dirname(binary_path),
            capture_output=True,
            text=True,
            timeout=15,
            env={"PATH": "/usr/bin:/bin"},
        )

        if result.returncode != 0:
            log.error("generator error for %s: %s", username, result.stderr.strip())
            return fallback_url

        output = result.stdout.strip()
        return output or fallback_url

    except subprocess.TimeoutExpired:
        log.error("generator timeout for %s", username)
        return fallback_url

    except OSError as e:
        log.error("generator exception for %s: %s", username, e)
        return fallback_url
