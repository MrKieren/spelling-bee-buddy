"""Where the NYT session cookie comes from.

Two sources; whichever was changed most recently wins:
  secrets/cookies.txt          edited by hand
  <profile>/admin-cookie.txt    written from the admin page
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

log = logging.getLogger("collector.session")

SESSION_COOKIE = "NYT-S"
PLACEHOLDER = "PASTE_VALUE_HERE"
COOKIES_FILE = Path(os.environ.get("COOKIES_FILE", "/secrets/cookies.txt"))
ADMIN_COOKIE_FILE = Path(os.environ.get("PROFILE_DIR", "/profile")) / "admin-cookie.txt"

# Printable ASCII with no whitespace, quotes, commas, semicolons or backslashes.
_VALID_VALUE = re.compile(r"^[!#-+\-./0-:<-\[\]-~]{20,4096}$")


def parse_cookies_txt(path: Path) -> list[dict]:
    """Netscape cookies.txt. Tolerates spaces instead of tabs, a BOM and CRLF."""
    cookies = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif not line or line.startswith("#"):
            continue
        parts = line.split(None, 6)  # any whitespace; the value is the rest of the line
        if len(parts) != 7:
            log.warning("%s: skipping line with %d fields (expected 7)", path.name, len(parts))
            continue
        domain, _, cpath, secure, expires, name, value = parts
        value = value.strip()
        if "nytimes.com" not in domain or value == PLACEHOLDER:
            continue
        c = {"name": name, "value": value, "domain": domain, "path": cpath,
             "secure": secure.upper() == "TRUE"}
        if expires.isdigit() and int(expires) > 0:
            c["expires"] = int(expires)
        cookies.append(c)
    return cookies


def valid_cookie_value(value: str) -> bool:
    return bool(_VALID_VALUE.match(value)) and value != PLACEHOLDER


def save_admin_cookie(value: str) -> None:
    """Store a cookie pasted into the admin page (atomic, owner-only)."""
    ADMIN_COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ADMIN_COOKIE_FILE.with_suffix(".tmp")
    tmp.write_text(
        "# Set from the admin page.\n"
        f".nytimes.com\tTRUE\t/\tTRUE\t2000000000\t{SESSION_COOKIE}\t{value}\n"
    )
    tmp.chmod(0o600)
    tmp.replace(ADMIN_COOKIE_FILE)


def current_cookies() -> tuple[list[dict], str | None]:
    """Cookies from the most recently changed usable source, and its name."""
    best: tuple[float, list[dict], str] | None = None
    for path in (COOKIES_FILE, ADMIN_COOKIE_FILE):
        try:
            cookies = parse_cookies_txt(path)
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            continue
        except Exception as exc:
            log.warning("Couldn't read %s: %s", path, exc)
            continue
        if cookies and (best is None or mtime > best[0]):
            best = (mtime, cookies, path.name)
    return (best[1], best[2]) if best else ([], None)
