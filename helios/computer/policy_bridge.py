"""What screen reading must never return: password fields, password-manager / credential-prompt
windows, and secret-shaped text. Applied by every reader before anything reaches the brain,
the logs or memory."""

from __future__ import annotations

import re

from .. import conf

# Processes whose window CONTENTS are never read (the window's existence/title may be reported).
SENSITIVE_PROCESSES = {
    "keepass.exe": "password manager", "keepassxc.exe": "password manager",
    "bitwarden.exe": "password manager", "1password.exe": "password manager",
    "lastpass.exe": "password manager", "dashlane.exe": "password manager",
    "nordpass.exe": "password manager", "enpass.exe": "password manager",
    "roboform.exe": "password manager", "keeperpasswordmanager.exe": "password manager",
    "protonpass.exe": "password manager",
    "credentialuibroker.exe": "Windows credential prompt", "consent.exe": "UAC prompt",
    "logonui.exe": "Windows sign-in", "lockapp.exe": "lock screen",
}
# Window titles that show stored credentials even inside ordinary apps (browser settings pages).
_SENSITIVE_TITLE = re.compile(
    r"(password manager|passwords? - (google chrome|microsoft edge|settings)|"
    r"chrome://password|edge://wallet|edge://settings/passwords|windows security|"
    r"credential manager|bitwarden|1password|keepass|lastpass)", re.I)


def sensitive_reason(app: str, title: str) -> str:
    """Why a window's contents must stay hidden ("" = fine to read)."""
    reason = SENSITIVE_PROCESSES.get((app or "").lower())
    if reason:
        return reason
    if _SENSITIVE_TITLE.search(title or ""):
        return "credentials page"
    return ""


def clean(text: str, limit: int = 300) -> str:
    """Secret-shaped strings redacted, whitespace collapsed, length capped."""
    t = conf.SECRET_RE.sub("[redacted]", str(text or ""))
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit] + ("…" if len(t) > limit else "")


def clean_block(text: str, limit: int) -> str:
    """Like clean() but keeps line breaks (document text)."""
    t = conf.SECRET_RE.sub("[redacted]", str(text or ""))
    t = re.sub(r"[ \t]+", " ", re.sub(r"\r\n?", "\n", t))
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t[:limit] + ("\n…" if len(t) > limit else "")
