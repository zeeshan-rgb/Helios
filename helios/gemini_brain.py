"""The Gemini brain: routes each turn to Google's Gemini CLI (signed in with the user's Google
account) in headless stream-json mode.

A drop-in for brain.Brain ([brain].engine = "gemini"): it inherits the public surface —
run_turn / panic / steering / new_conversation / set_session, the watchdog, tree-kill and memory
write-back — and replaces only the turn itself. Tool calls are gated by hooks/gemini_pretool.py,
which applies the exact same permission policy as the Claude brain's PreToolUse hook.
"""

from __future__ import annotations

import json
import subprocess
import threading

from . import agents, conf, db, gemini_cli, memory, router
from .brain import CREATE_NO_WINDOW, Brain


class GeminiBrain(Brain):
    """Owns the live Gemini CLI session (resumed by id each turn) and runs conversational turns."""

    def _system_prompt(self, prompt: str, servers: list[str]) -> str:
        system = self._persona
        try:
            tone = db.get_state("tone")
        except Exception:
            tone = None
        if tone:
            system += f"\n\nCURRENT TONE (the user adjusted this on the fly): {tone}"
        system += "\n\n" + memory.build_digest(prompt)
        system += "\n\n" + agents.orchestrator_brief()
        system += "\n\n" + gemini_cli.engine_note(servers)
        return system

    def _turn(self, message: str, record: bool = True, _retry: bool = False,
              interactive: bool = True, perm_sink: str = "", session_hold: dict | None = None):
        if not gemini_cli.command():
            self.emit("error", "The Gemini CLI isn't installed or isn't on PATH, sir. "
                               "See HELIOS_SETUP.md.")
            self.emit("done", {"text": ""})
            return ""
        resume_id = self.session_id if interactive else (session_hold or {}).get("id")
        used_resume = bool(resume_id)
        sess = {"id": resume_id}
        route = router.choose_model(message)
        model = gemini_cli.model_for(route["tier"], route["model"])
        self.emit("model", {"model": model, "tier": route["tier"], "reason": route["reason"]})
        prompt = route["message"].strip()
        if not prompt:
            self.emit("done", {"text": ""})
            return ""
        prompt = gemini_cli.escape_prompt(prompt)
        if prompt.startswith("/"):
            prompt = " " + prompt   # keep the CLI from treating it as a slash command

        extra = {"HELIOS_PERM_SINK": perm_sink} if perm_sink else {}
        servers = list(gemini_cli.mcp_servers())
        run = gemini_cli.prepare_run(self._system_prompt(prompt, servers), tools=True)
        args = gemini_cli.args(run, model, output="stream-json", resume=resume_id)
        conf.log("brain", f"gemini turn model={model} resume={resume_id} interactive={interactive}")
        try:
            proc = subprocess.Popen(
                args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=CREATE_NO_WINDOW, cwd=str(run["cwd"]),
                env=gemini_cli.env(run, extra),
            )
        except Exception as e:
            gemini_cli.cleanup(run)
            self.emit("error", f"Couldn't start the Gemini CLI: {e}")
            self.emit("done", {"text": ""})
            return ""
        with self._proc_lock:
            self.proc = proc
        try:
            proc.stdin.write(prompt + "\n")
            proc.stdin.close()
        except Exception:
            pass

        timeout = int(conf.SETTINGS.get("claude", {}).get("turn_timeout", 600))
        watchdog = threading.Timer(timeout, self._kill)
        watchdog.start()
        stderr_buf: list[str] = []
        drain = threading.Thread(target=self._drain_stderr, args=(stderr_buf,), daemon=True)
        drain.start()

        streamed: list[str] = []
        meta: dict = {"tools": [], "usage": {}, "errors": [], "seen": set(),
                      "servers": run["servers"], "gate_missing": False}
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Tools run in yolo mode, so refuse to go on unless the SessionStart marker proves
                # Helios's settings — and with them the BeforeTool permission gate — loaded.
                if obj.get("type") == "init" and not gemini_cli.gate_loaded(run):
                    meta["gate_missing"] = True
                    self._kill()
                    break
                self._handle_gemini_event(obj, streamed, sess, meta)
        finally:
            watchdog.cancel()
            proc.wait()
            drain.join(timeout=3)
            gemini_cli.cleanup(run)

        if interactive:
            self.session_id = sess["id"]
        elif session_hold is not None and sess["id"]:
            session_hold["id"] = sess["id"]
        final_text = "".join(streamed).strip()
        err_blob = "".join(stderr_buf)

        # A stale/unknown --resume id makes the CLI exit before answering: start fresh (once).
        if (used_resume and not _retry and not streamed
                and "error resuming session" in err_blob.lower()):
            conf.log("brain", "gemini: stale session id; retrying with a fresh session")
            if interactive:
                self.session_id = None
            if session_hold is not None:
                session_hold["id"] = None
            with self._proc_lock:
                self.proc = None
            return self._turn(message, record, _retry=True, interactive=interactive,
                              perm_sink=perm_sink, session_hold=session_hold)

        if meta["gate_missing"]:
            conf.log("brain", "gemini: permission gate did not load — turn killed")
            self.emit("error", "I stopped that turn, sir: Helios's permission gate didn't load in "
                               "the Gemini CLI, so tools would have run unchecked. See logs/brain.log.")
        elif not final_text and not self._aborted:
            detail = " ".join(meta["errors"]) or err_blob[-500:].strip()
            if gemini_cli.looks_unauthenticated(detail):
                self.emit("error", "Helios isn't signed in to Gemini yet, sir. Open a terminal "
                                   "and run `helios gemini-login`.")
            else:
                self.emit("error", detail or "No response from Gemini.")

        if interactive:
            self.emit("usage", {"model": model, "tier": route["tier"],
                                "tools": meta["tools"], **meta["usage"]})
        self.emit("done", {"text": final_text})

        if self._aborted:
            return final_text
        if interactive and final_text:
            light = conf.router_cfg().get("light", "haiku")
            threading.Thread(target=self._writeback,
                             args=(message, final_text, light), daemon=True).start()
            if any(str(t).startswith("mcp__computer__") for t in meta["tools"]):
                threading.Thread(target=memory.save_recipe, args=(message, meta["tools"]),
                                 daemon=True).start()
        if record and interactive and sess["id"]:
            try:
                db.upsert_conversation(sess["id"], title=message.strip()[:60])
                db.add_message(sess["id"], "user", message)
                db.add_message(sess["id"], "assistant", final_text or "")
            except Exception as e:  # pragma: no cover
                conf.log("brain", f"history persist failed: {e}")
        return final_text

    def _handle_gemini_event(self, obj: dict, streamed: list[str], sess: dict, meta: dict) -> None:
        """One stream-json event: init / message / tool_use / tool_result / error / result."""
        t = obj.get("type")
        if t == "init":
            sess["id"] = obj.get("session_id") or sess["id"]
        elif t == "message":
            if obj.get("role") == "assistant" and obj.get("content"):
                streamed.append(obj["content"])
                self.emit("token", obj["content"])
        elif t == "tool_use":
            tid = obj.get("tool_id") or ""
            if tid and tid in meta["seen"]:
                return
            meta["seen"].add(tid)
            name = claude_style_name(str(obj.get("tool_name") or "tool"), meta["servers"])
            meta["tools"].append(name)
            self.emit("tool", {"name": name})
        elif t == "tool_result":
            if obj.get("status") == "error":
                err = (obj.get("error") or {}).get("message", "")
                conf.log("brain", f"gemini tool error: {err[:300]}")
        elif t == "error":
            msg = str(obj.get("message") or "")
            conf.log("brain", f"gemini {obj.get('severity', 'error')}: {msg[:300]}")
            if obj.get("severity") == "error" and msg:
                meta["errors"].append(msg)
        elif t == "result":
            stats = obj.get("stats") or {}
            meta["usage"] = {"in": stats.get("input_tokens", 0) or 0,
                             "out": stats.get("output_tokens", 0) or 0,
                             "cost": None, "ms": stats.get("duration_ms")}
            if obj.get("status") == "error":
                err = (obj.get("error") or {}).get("message", "")
                if err:
                    meta["errors"].append(err)


def claude_style_name(name: str, servers: list[str]) -> str:
    """mcp_<server>_<tool> -> mcp__<server>__<tool> for known servers, so the dashboard timeline
    and procedural memory (which key on Claude-style names) work unchanged."""
    if name.startswith("mcp_"):
        for s in sorted(servers, key=len, reverse=True):
            prefix = f"mcp_{s}_"
            if name.startswith(prefix):
                return f"mcp__{s}__{name[len(prefix):]}"
    return name
