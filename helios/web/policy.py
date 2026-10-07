"""What the hidden browser may load and do.

* Loading: http/https only, never an internal address — checked for every request the page makes
  (sub-resources and redirects too), including hostnames that RESOLVE to a private address.
* Acting: reading, scrolling and ordinary clicks are free; anything that would act on someone's
  behalf (submit a POST form, send, post, buy, sign up, delete, email links) needs confirm=true,
  which goes to the user's Approve/Deny prompt — and background agents can't do it at all.
* Credentials: Helios never types into password, card or one-time-code fields.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
from urllib.parse import urlparse

from .. import permissions

_dns: dict[str, tuple[float, bool]] = {}
_dns_lock = threading.Lock()
_DNS_TTL = 600.0


def _resolves_internal(host: str, resolver=socket.getaddrinfo) -> bool:
    now = time.monotonic()
    with _dns_lock:
        hit = _dns.get(host)
        if hit and now - hit[0] < _DNS_TTL:
            return hit[1]
    try:
        infos = resolver(host, None)
        bad = False
        for info in infos:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved \
                    or ip.is_unspecified or ip.is_multicast:
                bad = True
                break
    except (socket.gaierror, UnicodeError, ValueError):
        bad = False                     # unresolvable: the browser will just fail to load it
    with _dns_lock:
        _dns[host] = (now, bad)
    return bad


def check_url(url: str, *, resolver=socket.getaddrinfo) -> str:
    """'' if the browser may load it, else the reason it's blocked."""
    try:
        u = urlparse(url)
    except ValueError:
        return "unparseable URL"
    if u.scheme in ("about", "data", "blob") and (u.scheme != "about" or url in ("about:blank",)):
        return ""
    if u.scheme not in ("http", "https"):
        return f"scheme {u.scheme or '(none)'}: not allowed"
    if not u.hostname:
        return "no host"
    if permissions.is_internal_url(url):
        return "internal address (blocked: SSRF protection)"
    if _resolves_internal(u.hostname.lower(), resolver):
        return "resolves to an internal address (blocked: SSRF protection)"
    return ""


# ------------------------------------------------------------------ actions

_RISKY = re.compile(
    r"\b(submit|send|post|publish|tweet|reply|comment|message|buy|purchase|pay|checkout|"
    r"check out|order|place order|subscribe|sign ?up|register|apply|delete|remove account|"
    r"confirm|donate|book now|reserve|transfer|upload|share|invite|follow|unsubscribe|"
    r"log ?out|sign ?out|accept|agree)\b", re.I)
_CREDENTIAL_AC = re.compile(r"(password|cc-|one-time-code|new-password|current-password)", re.I)


def risky_action(info: dict, *, pressing_enter: bool = False) -> str:
    """Why clicking / submitting this element could act on the user's behalf ('' = harmless).
    info = {tag, type, text, href, in_form, method} from the page."""
    if not info:
        return ""
    tag, typ = (info.get("tag") or "").lower(), (info.get("type") or "").lower()
    text = " ".join(str(info.get(k) or "") for k in ("text", "name")).strip()
    href = (info.get("href") or "").lower()
    posts = info.get("in_form") and (info.get("method") or "get").lower() == "post"
    if href.startswith(("mailto:", "sms:", "tel:")):
        return f"opens a {href.split(':')[0]} link (contacts someone)"
    if pressing_enter and posts:
        return "submits a form"
    if posts and (typ == "submit" or (tag == "button" and typ in ("", "submit"))):
        return "submits a form"
    m = _RISKY.search(text)
    if m and tag in ("button", "input", "a") or (m and info.get("role") in ("button", "link")):
        return f"looks like \"{m.group(0)}\""
    return ""


def credential_field(info: dict) -> bool:
    if not info:
        return False
    return (info.get("type") or "").lower() == "password" \
        or bool(_CREDENTIAL_AC.search(info.get("autocomplete") or ""))
