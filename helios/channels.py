"""Channels that reach the OWNER (never anyone else): Telegram self-messages. Shared by the
notify_tim tool and executive reporting. Configured in config/secrets.toml [telegram]."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

from . import conf


def telegram_target() -> tuple[str, str]:
    tg = conf.provider_cfg("telegram")
    token = str(tg.get("token") or "").strip()
    ids = tg.get("allowed_ids") or []
    chat = str(tg.get("chat_id") or (ids[0] if ids else "")).strip()
    return token, chat


def telegram_send(text: str, *, opener=None) -> tuple[bool, str]:
    """Send a message to the owner's own Telegram chat. (ok, detail). Never raises."""
    text = (text or "").strip()
    if not text:
        return False, "nothing to send"
    token, chat = telegram_target()
    if not (token and chat):
        return False, "Telegram isn't configured - set [telegram] token and allowed_ids in config/secrets.toml."
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000]}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with (opener or urllib.request.urlopen)(req, timeout=8) as r:
            out = json.loads(r.read() or b"{}")
        if out.get("ok"):
            return True, "sent"
        return False, f"Telegram rejected the message: {str(out.get('description'))[:120]}"
    except Exception as e:
        return False, f"Could not reach the Telegram bot API: {_clean(e)}"


def _clean(e) -> str:
    """Never echo the bot token (it's in the API URL) in an error."""
    token, _ = telegram_target()
    msg = str(e)
    return msg.replace(token, "[redacted]") if token else msg


def telegram_send_voice(audio: bytes, caption: str = "", *, opener=None) -> tuple[bool, str]:
    """Send an OGG/Opus voice message to the owner's own chat. (ok, message_id | reason)."""
    import uuid
    token, chat = telegram_target()
    if not (token and chat):
        return False, "Telegram isn't configured - run `helios telegram setup`."
    if not audio:
        return False, "nothing to send"
    b = f"----helios{uuid.uuid4().hex}"
    parts = [f'--{b}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat}\r\n'.encode()]
    if caption:
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="caption"\r\n\r\n{caption[:1000]}\r\n'.encode())
    parts += [f'--{b}\r\nContent-Disposition: form-data; name="voice"; filename="helios.ogg"\r\n'
              f'Content-Type: audio/ogg\r\n\r\n'.encode(), audio, f"\r\n--{b}--\r\n".encode()]
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendVoice", data=b"".join(parts),
                                     headers={"Content-Type": f"multipart/form-data; boundary={b}"})
        with (opener or urllib.request.urlopen)(req, timeout=60) as r:
            out = json.loads(r.read() or b"{}")
        if out.get("ok"):
            return True, str((out.get("result") or {}).get("message_id", ""))
        return False, f"Telegram rejected the voice message: {str(out.get('description'))[:120]}"
    except Exception as e:
        return False, f"Could not reach the Telegram bot API: {_clean(e)}"


def telegram_find_chat(token: str, *, opener=None) -> tuple[str, str]:
    """Setup helper: the newest private chat that messaged this bot → (chat_id, first_name)."""
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/getUpdates?timeout=0")
    try:
        with (opener or urllib.request.urlopen)(req, timeout=15) as r:
            out = json.loads(r.read() or b"{}")
    except Exception as e:
        raise ValueError(f"couldn't reach Telegram ({str(e).replace(token, '[redacted]')})") from None
    if not out.get("ok"):
        raise ValueError(f"Telegram refused the token ({out.get('description', 'unauthorized')})")
    for u in reversed(out.get("result") or []):
        chat = ((u.get("message") or {}).get("chat") or {})
        if chat.get("type") == "private":
            return str(chat["id"]), str(chat.get("first_name") or "")
    raise ValueError("no message found - open your bot in Telegram, press Start (or send 'hi'), then try again")
