"""The lite brain: a native, provider-agnostic turn loop for non-Claude engines.

A drop-in alternative to brain.Brain (same constructor + run_turn/panic/busy/new_conversation/
set_session surface + the same SSE event contract), used when [brain].engine == "lite". It talks
to any OpenAI-compatible provider (OpenRouter / OpenAI / Gemini / Ollama / Groq) via helios.llm,
streams tokens to the UI, and calls a CURATED, permission-gated toolset (helios.lite_tools) — the
seed of the eventual central multi-engine loop.

Differences vs the Claude brain (intentional, see handoff): no claude --resume (we persist & replay
our own message history per session via db), no MCP/computer-use/missions, no router (one configured
model). Memory injection + post-turn write-back are reused unchanged (write-back routes through
helios.llm so it works without Claude installed). Panic cancels the in-flight stream.
"""

from __future__ import annotations

import json
import threading
import time
import uuid

from . import conf, db, llm, memory
from .lite_tools import LiteTools

_TOOL_RESULT_CAP = 6000


class LiteBrain:
    """Owns the live conversation for a lite (non-Claude) engine and runs each turn."""

    def __init__(self, emit, perms=None):
        self.emit = emit
        self.perms = perms
        self.session_id: str | None = None
        self._lock = threading.Lock()           # single-flight: one turn at a time
        self._aborted = False                   # set by panic()/preempt(); skips post-turn persist
        self._current_message = None            # in-flight request, for mid-turn steering
        self._stream = None                     # active streaming response (so panic can cancel it)
        self._stream_lock = threading.Lock()
        self._persona = (conf.CONFIG_DIR / "persona_helios.md").read_text(encoding="utf-8")
        self._tools = LiteTools(emit=emit, perms=perms)
        self._tools_enabled = bool(conf.brain_cfg().get("tools_enabled", True))

    # --------------------------------------------------------------------- public API
    def busy(self) -> bool:
        return self._lock.locked()

    def _clear_yolo(self) -> None:
        try:
            conf.YOLO_FLAG.unlink(missing_ok=True)
        except Exception:
            pass
        self.emit("yolo", {"on": False})

    def new_conversation(self) -> None:
        self.session_id = None
        self._clear_yolo()
        self.emit("status", "New conversation started.")

    def set_session(self, sid: str | None) -> None:
        self.session_id = sid or None
        self._clear_yolo()
        self.emit("status", "Loaded a past conversation, sir." if sid else "New conversation started.")

    def run_turn(self, message: str, record: bool = True, interactive: bool = True,
                 steer: bool = False, perm_sink: str = "", session_hold: dict | None = None):
        """Run one turn. Mirrors Brain.run_turn (single-flight + steer/preempt + abort handling +
        perm_sink permission routing + session_hold per-caller continuity — same signature, so the
        Telegram bridge works on either engine)."""
        # Route this turn's permission asks to the caller's surface (e.g. telegram:<chat>). The
        # lite gate asks in-process via LiteTools -> perms.create, so hand the sink to the tools.
        try:
            self._tools.sink = perm_sink or ""
        except Exception:
            pass
        if not self._lock.acquire(blocking=False):
            if not steer:
                self.emit("error", "Helios is still working on the previous request, sir.")
                return None
            original = self._current_message or ""
            self.emit("status", "Noted — folding that in, sir.")
            self._preempt()
            if not self._lock.acquire(timeout=15):
                self.emit("error", "Couldn't fold that in, sir — the previous task wouldn't let go.")
                return None
            if original:
                message = (f"You were working on this request: {original!r}\n\n"
                           f"While you were working I added: {message!r}\n"
                           f"Take the addition into account and continue — adjust or redo as needed.")
        result = None
        self._aborted = False
        self._current_message = message
        try:
            if interactive:
                conf.ABORT_FLAG.unlink(missing_ok=True)
            result = self._turn(message, record, interactive=interactive, session_hold=session_hold)
        except Exception as e:  # pragma: no cover
            conf.log("lite", f"turn error: {e}")
            self.emit("error", f"Something went wrong: {e}")
        finally:
            self._current_message = None
            self._close_stream()
            self._lock.release()
        return result

    def panic(self) -> None:
        """Halt instantly: flag abort, cancel the in-flight stream, deny pending asks, drop YOLO."""
        self._aborted = True
        try:
            conf.ABORT_FLAG.parent.mkdir(parents=True, exist_ok=True)
            conf.ABORT_FLAG.write_text("stop", encoding="utf-8")
        except Exception:
            pass
        self._close_stream()
        if self.perms:
            self.perms.deny_all()
        self._clear_yolo()
        self.emit("status", "■ Stopped. Standing by, sir.")

    def _preempt(self) -> None:
        """Quietly stop the in-flight turn so a steering message can take over (no banner, keep YOLO)."""
        self._aborted = True
        try:
            conf.ABORT_FLAG.parent.mkdir(parents=True, exist_ok=True)
            conf.ABORT_FLAG.write_text("stop", encoding="utf-8")
        except Exception:
            pass
        self._close_stream()
        if self.perms:
            self.perms.deny_all()

    def _close_stream(self) -> None:
        with self._stream_lock:
            s = self._stream
        if s is not None:
            try:
                s.close()   # makes a blocking `for chunk in stream` raise -> treated as abort
            except Exception:
                pass

    # ------------------------------------------------------------------------ internals
    def _turn(self, message: str, record: bool = True, interactive: bool = True,
              session_hold: dict | None = None):
        # Mint our own continuity id (no claude --resume); interactive turns share it, background
        # turns run isolated (no session, no memory write-back) exactly like the Claude brain.
        # A background caller may pass its OWN session holder (e.g. a Telegram chat) — we mint an
        # id into it and replay/persist that chat's history so the phone is a real conversation.
        if interactive and not self.session_id:
            self.session_id = uuid.uuid4().hex
        if not interactive and session_hold is not None:
            if not session_hold.get("id"):
                session_hold["id"] = uuid.uuid4().hex
            sid = session_hold["id"]
        else:
            sid = self.session_id if interactive else None

        model = llm.lite_model()
        provider = conf.brain_cfg().get("provider", "lite")
        self.emit("model", {"model": model, "tier": provider, "reason": "lite engine"})

        prompt = message.strip()
        if not prompt:
            self.emit("done", {"text": ""})
            return ""

        try:
            client = llm.make_client()
        except Exception as e:
            self.emit("error", f"Couldn't reach the {provider} provider — is it configured? ({e})")
            return ""

        messages = [{"role": "system", "content": self._system_prompt(prompt)}]
        if sid:   # replay this session's history (desktop shared session OR a held phone session)
            try:
                for row in db.conversation_messages(sid):
                    role, content = row.get("role"), row.get("content") or ""
                    if role in ("user", "assistant") and content:
                        messages.append({"role": role, "content": content})
            except Exception as e:  # pragma: no cover
                conf.log("lite", f"history replay failed: {e}")
        messages.append({"role": "user", "content": prompt})

        tools = self._tools.schemas() if self._tools_enabled else None
        max_steps = int(conf.brain_cfg().get("max_steps", 8))
        timeout = int(conf.SETTINGS.get("claude", {}).get("turn_timeout", 600))

        final_text = ""
        tool_names: list[str] = []
        seen_tools: set[str] = set()
        total_in = total_out = 0
        t0 = time.time()

        for step in range(max_steps):
            if self._aborted:
                break
            try:
                text_parts, tool_acc, _finish, usage = self._stream_round(
                    client, model, messages, tools, timeout)
            except Exception as e:
                conf.log("lite", f"stream error: {e}")
                if not final_text:
                    self.emit("error", f"The {provider} request failed: {e}")
                break
            if usage:
                total_in += usage.get("in", 0)
                total_out += usage.get("out", 0)
            content_text = "".join(text_parts)
            if self._aborted:
                final_text = content_text or final_text
                break

            assistant_msg: dict = {"role": "assistant", "content": content_text or None}
            if tool_acc:
                assistant_msg["tool_calls"] = [
                    {"id": tc["id"] or f"call_{i}", "type": "function",
                     "function": {"name": tc["name"], "arguments": tc["args"] or "{}"}}
                    for i, tc in sorted(tool_acc.items())
                ]
            messages.append(assistant_msg)

            if not tool_acc:                 # final text, no tools -> done
                final_text = content_text or final_text
                break

            for i, tc in sorted(tool_acc.items()):
                if self._aborted:
                    break
                name = tc["name"] or ""
                key = tc["id"] or name
                if key not in seen_tools:
                    seen_tools.add(key)
                    tool_names.append(name)
                    self.emit("tool", {"name": name})
                try:
                    args = json.loads(tc["args"]) if tc["args"] else {}
                    if not isinstance(args, dict):
                        args = {}
                except Exception:
                    args = {}
                result = self._tools.execute(name, args)
                messages.append({"role": "tool", "tool_call_id": tc["id"] or f"call_{i}",
                                 "content": str(result)[:_TOOL_RESULT_CAP]})
            # loop: the model now sees the tool results

        if interactive:
            self.emit("usage", {"model": model, "tier": provider, "tools": tool_names,
                                "in": total_in, "out": total_out, "cost": None,
                                "ms": int((time.time() - t0) * 1000)})
        self.emit("done", {"text": final_text})

        if self._aborted:
            return final_text
        if interactive and final_text:
            threading.Thread(target=self._writeback, args=(message, final_text), daemon=True).start()
        # Persist history for the shared desktop session (record) AND for a held background session
        # (its continuity depends on replaying these rows) — but only desktop sessions get a
        # conversations row, so phone chats never clutter the dashboard's history list.
        if sid and ((record and interactive) or (not interactive and session_hold is not None)):
            try:
                if record and interactive:
                    db.upsert_conversation(sid, title=message.strip()[:60])
                db.add_message(sid, "user", message)
                db.add_message(sid, "assistant", final_text or "")
            except Exception as e:  # pragma: no cover
                conf.log("lite", f"history persist failed: {e}")
        return final_text

    def _stream_round(self, client, model, messages, tools, timeout):
        """One streaming completion. Emits 'token' deltas; accumulates fragmented tool_calls by
        index. Returns (text_parts, tool_acc{index:{id,name,args}}, finish_reason, usage|None)."""
        kwargs = dict(model=model, messages=messages, stream=True, timeout=timeout)
        if tools:
            kwargs["tools"] = tools
        try:
            stream = client.chat.completions.create(
                stream_options={"include_usage": True}, **kwargs)
        except TypeError:
            stream = client.chat.completions.create(**kwargs)   # provider ignores stream_options
        except Exception as e:
            if tools and self._looks_like_tool_error(e):
                conf.log("lite", f"provider rejected tools; retrying without: {e}")
                kwargs.pop("tools", None)
                stream = client.chat.completions.create(**kwargs)
            else:
                raise
        with self._stream_lock:
            self._stream = stream

        text_parts: list[str] = []
        tool_acc: dict[int, dict] = {}
        finish = None
        usage = None
        try:
            for chunk in stream:
                if self._aborted:
                    break
                u = getattr(chunk, "usage", None)
                if u is not None:
                    usage = {"in": getattr(u, "prompt_tokens", 0) or 0,
                             "out": getattr(u, "completion_tokens", 0) or 0}
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                ch = choices[0]
                if getattr(ch, "finish_reason", None):
                    finish = ch.finish_reason
                delta = getattr(ch, "delta", None)
                if not delta:
                    continue
                content = getattr(delta, "content", None)
                if content:
                    text_parts.append(content)
                    self.emit("token", content)
                for tc in (getattr(delta, "tool_calls", None) or []):
                    idx = getattr(tc, "index", 0) or 0
                    slot = tool_acc.setdefault(idx, {"id": "", "name": "", "args": ""})
                    if getattr(tc, "id", None):
                        slot["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn is not None:
                        if getattr(fn, "name", None):
                            slot["name"] = fn.name
                        if getattr(fn, "arguments", None):
                            slot["args"] += fn.arguments
        finally:
            try:
                stream.close()
            except Exception:
                pass
            with self._stream_lock:
                self._stream = None
        return text_parts, tool_acc, finish, usage

    @staticmethod
    def _looks_like_tool_error(e: Exception) -> bool:
        s = str(e).lower()
        return ("tool" in s or "function" in s) and ("support" in s or "invalid" in s
                                                      or "not" in s or "400" in s)

    def _system_prompt(self, message: str) -> str:
        system = self._persona
        try:
            tone = db.get_state("tone")
        except Exception:
            tone = None
        if tone:
            system += f"\n\nCURRENT TONE (the user adjusted this on the fly): {tone}"
        system += "\n\n" + memory.build_digest(message)
        system += "\n\n" + self._lite_note()
        return system

    def _lite_note(self) -> str:
        has_search = self._tools.search_enabled
        return (
            "## LITE MODE\n"
            "You are running on a non-Claude engine with a LIMITED, fixed toolset. IGNORE any "
            "earlier instructions about driving the GUI by element index, cua-driver, screenshots, "
            "side agents, or missions — those are NOT available here. Your ONLY tools are the "
            "functions provided in this request: read/write a file, run a PowerShell command, open "
            "an app, fetch a URL"
            + (", search the web" if has_search else "")
            + ", check system health, recall/save memory, and manage reminders. "
            + ("" if has_search else
               "You have NO web search — if you need current web info, ask the user for a URL and use "
               "web_fetch. ")
            + "Some actions ask the user for approval; if one is denied, adapt and say so. Keep replies "
              "concise and in Helios's calm butler voice."
        )

    def _writeback(self, message: str, reply: str) -> None:
        try:
            status = memory.extract_and_write(message, reply, llm.lite_model())
            self.emit("memory", status)
        except Exception as e:  # pragma: no cover
            conf.log("lite", f"writeback error: {e}")
