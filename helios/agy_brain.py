"""The Antigravity brain: Helios's conversational turns run on Google's official Antigravity CLI
(`agy`), signed in with the user's Google account ([brain].engine = "antigravity").

A drop-in for brain.Brain: it inherits run_turn / steering / new_conversation / set_session, the
watchdog and tree-kill, and replaces only the turn. The live chat keeps ONE persistent
`agy --input-format stream-json` session (agy's startup is ~10s, so a warm session keeps voice
replies fast); background turns (routines, Telegram-held chats) use one-shot `agy -p` runs.
Tool calls are gated by hooks/agy_pretool.py — the same Helios policy as the Claude brain.
"""

from __future__ import annotations

import hashlib
import threading

from . import agents, agy_cli, conf, db, memory, router
from .brain import Brain


class AntigravityBrain(Brain):
    def __init__(self, emit, perms=None):
        super().__init__(emit, perms)
        self._session: agy_cli.AgySession | None = None
        self._session_key: str | None = None

    # ------------------------------------------------------------------ context
    def _rules(self) -> str:
        """Session-level instructions (agy reads rules at session start)."""
        rules = self._persona
        try:
            tone = db.get_state("tone")
        except Exception:
            tone = None
        if tone:
            rules += f"\n\nCURRENT TONE (the user adjusted this on the fly): {tone}"
        rules += "\n\n" + agents.orchestrator_brief()
        rules += "\n\n" + agy_cli.engine_note(sorted(agy_cli.mcp_servers()))
        return rules

    def _with_memory(self, prompt: str) -> str:
        """Per-turn memory digest, capped: it becomes part of the conversation history, so a big
        digest every turn would burn the account's quota."""
        cap = int(agy_cli.cfg().get("digest_chars", 4000) or 0)
        digest = memory.build_digest(prompt) if cap else ""
        if len(digest) > cap:
            digest = digest[:cap] + "\n…(memory truncated)"
        body = agy_cli.neutralize(prompt)
        if not digest:
            return body
        return f"<helios_memory>\n{agy_cli.neutralize(digest)}\n</helios_memory>\n\n{body}"

    # ------------------------------------------------------------------ session lifecycle
    def _close_session(self) -> None:
        s, self._session, self._session_key = self._session, None, None
        if s is not None:
            s.close()

    def _ensure_session(self, model: str, resume: str | None) -> agy_cli.AgySession:
        rules = self._rules()
        key = hashlib.sha256(f"{model}\n{rules}".encode("utf-8")).hexdigest()
        s = self._session
        if s is not None and s.alive() and self._session_key == key and \
                (resume is None or s.conversation_id == resume):
            return s
        self._close_session()
        handle = agy_cli.prepare_workspace(agy_cli.WORKSPACE, rules)
        s = agy_cli.AgySession(handle, model, resume=resume, sink_file=agy_cli.SINK_FILE)
        s.start()
        self._session, self._session_key = s, key
        conf.log("brain", f"agy session started model={model or 'default'} resume={resume}")
        return s

    def warm(self) -> None:
        """Start the live agy session in the background (app boot) so the first voice turn doesn't
        pay agy's cold start. Skips if a turn is already running."""
        if not agy_cli.command() or not self._lock.acquire(blocking=False):
            return
        try:
            self._ensure_session(agy_cli.model_for("fixed"), self.session_id)
        except Exception as e:
            conf.log("brain", f"agy warm-up failed: {e}")
        finally:
            self._lock.release()

    def new_conversation(self) -> None:
        self._close_session()
        super().new_conversation()

    def set_session(self, sid: str | None) -> None:
        self._close_session()
        super().set_session(sid)

    def panic(self) -> None:
        self._close_session()   # also stops background commands agy left running between turns
        super().panic()

    # ------------------------------------------------------------------ the turn
    def _turn(self, message: str, record: bool = True, _retry: bool = False,
              interactive: bool = True, perm_sink: str = "", session_hold: dict | None = None):
        if not agy_cli.command():
            self.emit("error", "The Antigravity CLI (agy) isn't installed or configured, sir. "
                               "See HELIOS_SETUP.md.")
            self.emit("done", {"text": ""})
            return ""
        route = router.choose_model(message)
        live_model = agy_cli.model_for("fixed")            # the persistent session's model
        model = live_model if interactive else agy_cli.model_for(route["tier"], route["model"])
        self.emit("model", {"model": model or "agy default", "tier": route["tier"],
                            "reason": route["reason"]})
        prompt = route["message"].strip()
        if not prompt:
            self.emit("done", {"text": ""})
            return ""
        prompt = self._with_memory(prompt)

        resume = self.session_id if interactive else (session_hold or {}).get("id")
        timeout = int(conf.SETTINGS.get("claude", {}).get("turn_timeout", 600))
        conf.log("brain", f"agy turn interactive={interactive} resume={resume}")
        on_token = lambda t: self.emit("token", t)
        on_tool = lambda n: self.emit("tool", {"name": n})

        if interactive:
            try:
                agy_cli.SINK_FILE.parent.mkdir(parents=True, exist_ok=True)
                agy_cli.SINK_FILE.write_text(perm_sink or "", encoding="utf-8")
                session = self._ensure_session(live_model, resume)
            except Exception as e:
                self.emit("error", f"Couldn't start the Antigravity CLI: {e}")
                self.emit("done", {"text": ""})
                return ""
            with self._proc_lock:
                self.proc = session.proc       # panic / steering / watchdog kill this session
            watchdog = threading.Timer(timeout, self._close_session)
            watchdog.start()
            try:
                session.reset_marker()
                session.send(prompt)
                res = agy_cli.consume_turn(session, on_token=on_token, on_tool=on_tool)
            except (BrokenPipeError, OSError, ValueError) as e:
                res = {"text": "", "errors": [f"session error: {e}"], "gate_failure": None,
                       "conversation_id": session.conversation_id, "status": None,
                       "usage": {}, "tools": []}
            finally:
                watchdog.cancel()
            stderr = session.stderr_text() if not session.alive() else ""
            if res.get("gate_failure") or not session.alive():
                self._close_session()
            # A stale --conversation id: the session dies before answering. Start fresh once.
            if resume and not _retry and not res["text"] and not res.get("gate_failure") \
                    and not self._aborted and res.get("status") is None:
                conf.log("brain", "agy: resume failed; retrying with a fresh conversation")
                self.session_id = None
                with self._proc_lock:
                    self.proc = None
                return self._turn(message, record, _retry=True, interactive=interactive,
                                  perm_sink=perm_sink, session_hold=session_hold)
            self.session_id = res.get("conversation_id") or self.session_id
        else:
            def register(p):
                with self._proc_lock:
                    self.proc = p

            res = agy_cli.run_once(prompt, self._rules(), model=model, resume=resume,
                                   extra_env={"HELIOS_PERM_SINK": perm_sink} if perm_sink else None,
                                   timeout=timeout, label="background turn", register=register)
            stderr = ""
            if session_hold is not None and res.get("conversation_id"):
                session_hold["id"] = res["conversation_id"]
            for t in res.get("tools", []):
                on_tool(t)
            if res.get("text"):
                on_token(res["text"])

        final_text = res.get("text") or ""
        if res.get("gate_failure"):
            conf.log("brain", f"agy: permission gate failure ({res['gate_failure']}) — turn killed")
            self.emit("error", "I stopped that turn, sir: Helios's permission gate "
                               f"{'did not load' if res['gate_failure'] == 'marker' else 'was modified'}"
                               " in the Antigravity agent, so tools would have run unchecked.")
        elif not final_text and not self._aborted:
            detail = " ".join(res.get("errors") or []) or stderr[-500:].strip()
            if agy_cli.looks_unauthenticated(detail):
                self.emit("error", "The Antigravity CLI isn't signed in, sir. Open a terminal, "
                                   "run `agy`, and sign in with Google.")
            else:
                self.emit("error", detail or "No response from the Antigravity agent.")

        if interactive:
            self.emit("usage", {"model": model or "agy default", "tier": route["tier"],
                                "tools": res.get("tools", []), **(res.get("usage") or {})})
        self.emit("done", {"text": final_text})

        if self._aborted:
            return final_text
        sid = self.session_id if interactive else (session_hold or {}).get("id")
        if interactive and final_text:
            if agy_cli.cfg().get("memory_writeback", False):
                light = conf.router_cfg().get("light", "haiku")
                threading.Thread(target=self._writeback, args=(message, final_text, light),
                                 daemon=True).start()
            else:   # no extra agy run: just keep the day's log (Night Mode distills it later)
                threading.Thread(target=memory.log_exchange, args=(message, final_text),
                                 daemon=True).start()
            if any(str(t).startswith("mcp__computer__") for t in res.get("tools", [])):
                threading.Thread(target=memory.save_recipe, args=(message, res["tools"]),
                                 daemon=True).start()
        if record and interactive and sid:
            try:
                db.upsert_conversation(sid, title=message.strip()[:60])
                db.add_message(sid, "user", message)
                db.add_message(sid, "assistant", final_text or "")
            except Exception as e:  # pragma: no cover
                conf.log("brain", f"history persist failed: {e}")
        return final_text
