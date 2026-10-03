import logging
import os
import re
import subprocess
import time

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


def generate_link(username: str, domain: str, retries: int = 3, retry_delay: float = 0.7) -> str:
    """
    BUG FIX: even with core.service.restart_trusttunnel()'s new
    is-active poll, the binary can still occasionally not know about a
    brand-new username the first time it's called right after a resync
    (readiness of the systemd unit isn't a perfect proxy for the binary
    having fully re-read vpn.toml/hosts.toml). Right after issuing a new
    user, a non-zero exit or empty stdout is far more likely "not ready
    yet" than a permanent error, so retry a few times with a short delay
    before giving up and handing back the fallback link. A genuinely
    missing binary (resolve_endpoint_binary() -> None) is NOT retried —
    that's a permanent config issue, not a timing one.
    """
    validate_domain(domain)

    binary_path = resolve_endpoint_binary()

    fallback_url = f"https://{domain}/connect/{username}"

    if not binary_path:
        return fallback_url

    cmd = [binary_path, "vpn.toml", "hosts.toml", "-c", username, "-a", domain]

    for attempt in range(1, retries + 1):
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
                if attempt < retries:
                    time.sleep(retry_delay)
                    continue
                log.error("generator error for %s: %s", username, result.stderr.strip())
                return fallback_url

            output = result.stdout.strip()
            if output:
                return output

            # Zero exit code but empty stdout -- same "doesn't know about
            # this username yet" symptom, treated the same way.
            if attempt < retries:
                time.sleep(retry_delay)
                continue
            return fallback_url

        except subprocess.TimeoutExpired:
            log.error("generator timeout for %s", username)
            return fallback_url

        except OSError as e:
            log.error("generator exception for %s: %s", username, e)
            return fallback_url

    return fallback_url
