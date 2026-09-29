"""Text Helios from your phone via a Telegram bot.

Long-polls Telegram (stdlib only), routes each authorized message through the brain, and
sends the reply back. Inert unless [telegram] token is set in settings.toml.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request

from . import conf

_API = "https://api.telegram.org/bot{token}/{method}"


class TelegramBridge:
    """Long-poll Telegram bridge: relays messages from authorized chats into the brain.

    SECURITY: `allowed` is a strict allowlist of chat ids. An empty allowlist means
    DENY-ALL (every message is rejected) — this is deliberate, NOT a bug: a blank/missing
    allowlist must never grant the whole world remote control of the user's PC."""

    def __init__(self, brain, token: str, allowed_ids, perms=None):
        self.brain = brain
        self.token = token
        # Normalise ids to strings so comparison with Telegram's chat ids is type-stable.
        self.allowed = {str(a) for a in (allowed_ids or [])}
        # Permission broker: register ourselves so an ask from a Telegram-started turn surfaces
        # here (Approve/Deny buttons) instead of only on the desktop dashboard.
        self.perms = perms
        if perms is not None:
            perms.telegram_asker = self.send_permission_ask
        # Per-chat session holders ({chat_id: {"id": <session>}}) so each phone chat is a
        # CONTINUOUS conversation (follow-ups work) while staying isolated from the desktop
        # session. /new (or /reset) drops the holder to start that chat fresh.
        self._sessions: dict[str, dict] = {}
        self._stop = threading.Event()
        self._offset = 0

    def start(self) -> None:
        """Start the poll loop on a daemon thread. No-op when no token is configured, so
        the bridge stays inert unless [telegram] is set up in settings."""
        if not self.token:
            return
        threading.Thread(target=self._loop, daemon=True).start()
        conf.log("telegram", "bridge started")

    def stop(self) -> None:
        """Signal the poll loop to exit."""
        self._stop.set()

    def _api(self, method: str, **params):
        """Call a Telegram Bot API method via stdlib urllib; returns the parsed JSON.
        45s socket timeout comfortably exceeds the 30s long-poll used in getUpdates."""
        url = _API.format(token=self.token, method=method)
        data = urllib.parse.urlencode(params).encode()
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=45) as r:
            return json.loads(r.read())

    def _send(self, chat_id, text: str) -> None:
        """Send a text reply to a chat (capped at Telegram's ~4096-char message limit)."""
        try:
            self._api("sendMessage", chat_id=chat_id, text=(text or "(no reply)")[:4000])
        except Exception as e:  # pragma: no cover
            conf.log("telegram", f"send failed: {e}")

    def _loop(self) -> None:
        """Long-poll getUpdates forever, dispatching each authorized text message to the
        brain and replying with the result. Errors back off 5s and retry."""
        while not self._stop.is_set():
            try:
                res = self._api("getUpdates", offset=self._offset, timeout=30,
                                allowed_updates=json.dumps(["message", "callback_query"]))
                for u in res.get("result", []):
                    uid = u.get("update_id")
                    if uid is None:
                        continue
                    # Advance offset past this update so Telegram won't redeliver it. Done before
                    # handling so a single poison/malformed update can't wedge the loop in a
                    # 5s crash-retry cycle; per-update try/except isolates handler failures.
                    self._offset = uid + 1
                    try:
                        # An inline Approve/Deny button tap (resolves a pending permission ask).
                        if "callback_query" in u:
                            self._handle_callback(u["callback_query"])
                            continue
                        msg = u.get("message") or {}
                        chat = str(msg.get("chat", {}).get("id", ""))
                        text = (msg.get("text") or "").strip()
                        if not text:
                            continue
                        # Deny-all when no allowlist is configured (an empty allowlist must
                        # NOT mean "anyone can control the PC").
                        if not self.allowed or chat not in self.allowed:
                            self._send(chat, f"Not authorized. (your id: {chat}) — add it to "
                                             f"allowed_ids in config/secrets.toml, then restart Helios.")
                            continue
                        if text.lower() in ("/start", "/help"):
                            self._send(chat, "Helios at your service, sir. Just message me normally — "
                                             "I'll handle it on your PC and reply here. I remember our "
                                             "conversation; send /new to start fresh.")
                            continue
                        if text.lower() in ("/new", "/reset"):
                            self._sessions.pop(chat, None)
                            self._send(chat, "Fresh conversation, sir.")
                            continue
                        self._send(chat, "On it, sir…")
                        # Run the turn on its OWN thread so the poll loop stays free to receive the
                        # Approve/Deny button tap while the turn is blocked waiting on a permission
                        # (otherwise: deadlock — the turn waits for the user, but the reply can't be read).
                        threading.Thread(target=self._run_turn_and_reply, args=(chat, text),
                                         daemon=True).start()
                    except Exception as e:  # one bad update must not abort the whole batch
                        conf.log("telegram", f"update {uid} failed: {e}")
            except Exception as e:  # pragma: no cover
                conf.log("telegram", f"loop error: {e}")
                time.sleep(5)

    def _run_turn_and_reply(self, chat: str, text: str) -> None:
        """Run one phone turn and send its reply. Runs off the poll loop (see _loop). perm_sink
        routes any permission ask back to THIS chat as Approve/Deny buttons.

        interactive=False keeps phone turns out of the desktop's shared session_id and its memory
        write-back; session_hold gives THIS chat its own persistent session instead, so the phone
        is a continuous conversation (follow-ups work) that's still isolated from the desktop."""
        try:
            hold = self._sessions.setdefault(chat, {"id": None})
            reply = self.brain.run_turn(text, record=False, interactive=False,
                                        perm_sink=f"telegram:{chat}", session_hold=hold)
        except Exception as e:
            conf.log("telegram", f"turn failed: {e}")
            self._send(chat, "Something went wrong handling that, sir.")
            return
        if reply is None:   # brain was busy with another turn
            self._send(chat, "I'm tied up with another task right now, sir — try again in a moment.")
        else:
            self._send(chat, reply or "Done.")

    def send_permission_ask(self, rid: str, chat_id, summary: str) -> None:
        """Surface a permission Approve/Deny prompt in a Telegram chat. Called by the permission
        broker (PendingRegistry) when a Telegram-started turn hits a gated tool. The inline-button
        callback_data carries the request id so a tap resolves exactly that ask."""
        kb = json.dumps({"inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"perm:allow:{rid}"},
            {"text": "\U0001f6ab Deny", "callback_data": f"perm:deny:{rid}"}]]})
        try:
            self._api("sendMessage", chat_id=chat_id,
                      text=f"\U0001f510 Permission needed, sir:\n{(summary or '')[:600]}",
                      reply_markup=kb)
        except Exception as e:  # pragma: no cover
            conf.log("telegram", f"perm ask send failed: {e}")

    def _handle_callback(self, cq: dict) -> None:
        """Resolve a pending permission ask from an inline-button tap, then reflect the decision."""
        data = str(cq.get("data", "") or "")
        cq_id = cq.get("id")
        frm = str((cq.get("from") or {}).get("id", ""))
        msg = cq.get("message") or {}
        chat = str((msg.get("chat") or {}).get("id", ""))
        # Only an authorized chat/user may approve or deny (same allowlist as messages).
        if not self.allowed or (chat not in self.allowed and frm not in self.allowed):
            try:
                self._api("answerCallbackQuery", callback_query_id=cq_id, text="Not authorized")
            except Exception:
                pass
            return
        if not data.startswith("perm:"):
            return
        try:
            _, decision, rid = data.split(":", 2)
        except ValueError:
            return
        resolved = bool(self.perms and self.perms.resolve(rid, "allow" if decision == "allow" else "deny"))
        label = "✅ Approved" if decision == "allow" else "\U0001f6ab Denied"
        try:
            self._api("answerCallbackQuery", callback_query_id=cq_id,
                      text=(label if resolved else "That request has expired."))
            if msg.get("message_id"):   # drop the buttons + show the outcome
                self._api("editMessageText", chat_id=chat, message_id=msg["message_id"],
                          text=(label if resolved else "⏱ That permission request expired."))
        except Exception as e:  # pragma: no cover
            conf.log("telegram", f"callback ui failed: {e}")
