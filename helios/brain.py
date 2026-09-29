"""The brain: routes each turn to the Claude Code CLI in headless streaming mode.

Keeps one resumable Claude session for conversational continuity, streams tokens and
tool activity to the UI, picks a model per task (router), and after each reply kicks
off background memory extraction into the Obsidian vault. Panic stops it instantly.
"""

from __future__ import annotations

import json
import subprocess
import threading

from . import agents, conf, db, memory, router

# Windows CreateProcess flag: spawn the claude CLI / taskkill with no console window.
# Required because the app runs under pythonw (no console) — without this a black
# console window would flash on screen for every subprocess.
CREATE_NO_WINDOW = 0x08000000


class Brain:
    """Owns the single live Claude-Code session and runs each conversational turn.

    One Brain instance drives the assistant. run_turn() shells out to the Claude Code CLI
    (`claude -p`, see conf.CLAUDE_BIN) in headless streaming mode, parses its stream-json
    output, and pushes tokens/tool-activity to the UI via the `emit` callback. It keeps a
    resumable session_id for continuity (conf/db side), routes each message to a model
    (router.choose_model), injects persona + memory digest + orchestrator brief into the
    system prompt, and after each interactive reply spawns background memory write-back
    (memory.extract_and_write). panic() stops everything instantly. A lock enforces
    single-flight (one turn at a time).
    """

    def __init__(self, emit, perms=None):
        """Wire up a Brain.

        emit: callable(kind:str, data) used to push events to the UI — kinds include
              'token', 'tool', 'model', 'status', 'memory', 'done', 'error'.
        perms: optional permission manager; panic() calls its deny_all() to reject any
               pending tool-approval asks.
        """
        self.emit = emit                 # callable(kind:str, data) -> push to UI
        self.perms = perms
        self.session_id: str | None = None   # Claude --resume id for the live conversation
        self.proc: subprocess.Popen | None = None  # the currently-running claude -p process
        self._lock = threading.Lock()       # single-flight: one turn at a time
        self._proc_lock = threading.Lock()  # guards self.proc across turn/watchdog/panic threads
        self._aborted = False               # set by panic(); skips this turn's post-turn persistence
        self._current_message = None        # the in-flight turn's request, for mid-turn steering
        # Persona/system-prompt prefix (the butler "Helios" persona), loaded once at startup.
        self._persona = (conf.CONFIG_DIR / "persona_helios.md").read_text(encoding="utf-8")

    # --------------------------------------------------------------------- public API
    def busy(self) -> bool:
        """True if a turn is currently running (the single-flight lock is held)."""
        return self._lock.locked()

    def _clear_yolo(self) -> None:
        """Turn YOLO mode off (its file flag is per-chat). Called on every conversation boundary so
        all-permissions never silently carries into a new/loaded chat or survives a panic."""
        try:
            conf.YOLO_FLAG.unlink(missing_ok=True)
        except Exception:
            pass
        self.emit("yolo", {"on": False})   # sync the dashboard toggle off

    def new_conversation(self) -> None:
        """Forget the resume id so the next turn starts a brand-new Claude session."""
        self.session_id = None
        self._clear_yolo()
        self.emit("status", "New conversation started.")

    def set_session(self, sid: str | None) -> None:
        """Switch the live conversation to a past session id (or None to start fresh) —
        used when the user loads an earlier conversation from history."""
        self.session_id = sid or None
        self._clear_yolo()
        self.emit("status", "Loaded a past conversation, sir." if sid else "New conversation started.")

    def run_turn(self, message: str, record: bool = True, interactive: bool = True,
                 steer: bool = False, perm_sink: str = "", session_hold: dict | None = None):
        """Run one turn. Returns the final assistant text, or None if the brain was busy.

        interactive=True turns (UI/Telegram) share and update the conversation session
        and write memory back. Background turns (routines/jobs/screen checks) pass
        interactive=False so they run in an isolated, throwaway session and never touch
        the user's conversation or memory.

        steer=True (a dashboard message sent mid-turn): if a turn is already running, don't
        reject it — quietly preempt the live turn and take over with a prompt that merges the
        ORIGINAL request with this addition, so Helios folds the new instruction in instead of
        ignoring it or restarting cold. (claude -p is one-shot, so this is preempt-and-merge, not
        true in-flight injection; the preempted turn's uncommitted partial work is dropped.)
        """
        if not self._lock.acquire(blocking=False):
            if not steer:
                self.emit("error", "Helios is still working on the previous request, sir.")
                return None
            original = self._current_message or ""   # capture BEFORE preempting (the old turn clears it)
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
        self._aborted = False  # fresh turn; panic() flips this to skip post-turn persistence
        self._current_message = message   # so a steering message can merge it (cleared in finally)
        try:
            # Clear a lingering panic only when the user actively re-engages — a scheduled
            # routine firing after a panic must NOT silently undo the stop. (A steer preempt sets
            # the abort flag too; this same line clears it for the taking-over turn.)
            if interactive:
                conf.ABORT_FLAG.unlink(missing_ok=True)
            result = self._turn(message, record, interactive=interactive, perm_sink=perm_sink,
                                session_hold=session_hold)
        except Exception as e:  # pragma: no cover
            conf.log("brain", f"turn error: {e}")
            self.emit("error", f"Something went wrong: {e}")
        finally:
            self._current_message = None
            with self._proc_lock:
                self.proc = None
            self._lock.release()
        return result

    def panic(self) -> None:
        """Halt instantly: flag the computer-use server, kill the brain, deny pending asks."""
        self._aborted = True  # so an in-flight turn skips its post-turn memory/DB write-back
        try:
            conf.ABORT_FLAG.parent.mkdir(parents=True, exist_ok=True)
            conf.ABORT_FLAG.write_text("stop", encoding="utf-8")
        except Exception:
            pass
        self._kill()
        if self.perms:
            self.perms.deny_all()
        self._clear_yolo()   # a hard stop also drops all-permissions back to guarded
        self.emit("status", "■ Stopped. Standing by, sir.")

    def _preempt(self) -> None:
        """Quietly stop the in-flight turn so a STEERING message can take over — like panic() but
        with NO '■ Stopped' banner and WITHOUT clearing YOLO (the same chat continues). The next
        interactive turn unlinks the abort flag we set here, so the taking-over turn runs clean."""
        self._aborted = True
        try:
            conf.ABORT_FLAG.parent.mkdir(parents=True, exist_ok=True)
            conf.ABORT_FLAG.write_text("stop", encoding="utf-8")
        except Exception:
            pass
        self._kill()
        if self.perms:
            self.perms.deny_all()

    # ------------------------------------------------------------------------ internals
    def _turn(self, message: str, record: bool = True, _retry: bool = False,
              interactive: bool = True, perm_sink: str = "", session_hold: dict | None = None):
        """The actual turn body (run_turn() wraps this with the lock + error handling).

        Builds the system prompt, launches `claude -p`, streams its output, and returns the
        final assistant text. _retry is set on the single re-run we allow when a stale
        --resume id is detected (see the bottom of this method). See run_turn() for the
        meaning of `record` and `interactive`.
        """
        # Only interactive turns resume/commit the SHARED conversation session. A background caller
        # may pass its OWN session holder (session_hold={"id": ...}) for continuity that stays fully
        # isolated from the desktop session — e.g. a persistent per-chat Telegram conversation.
        if interactive:
            resume_id = self.session_id
        else:
            resume_id = (session_hold or {}).get("id")
        used_resume = bool(resume_id)
        sess = {"id": resume_id}   # session state for THIS turn (a local holder)
        route = router.choose_model(message)
        self.emit("model", {"model": route["model"], "tier": route["tier"],
                            "reason": route["reason"]})
        prompt = route["message"].strip()
        if not prompt:
            self.emit("done", {"text": ""})
            return ""
        if prompt.startswith("/"):
            prompt = " " + prompt   # keep claude -p from treating it as a slash command

        system = self._persona
        try:
            tone = db.get_state("tone")
        except Exception:
            tone = None
        if tone:
            system += f"\n\nCURRENT TONE (the user adjusted this on the fly): {tone}"
        system += "\n\n" + memory.build_digest(prompt)
        # Orchestrator-only: tell the live brain about its specialist side agents. Side agents
        # (run_task) never get this, so they don't try to delegate further.
        system += "\n\n" + agents.orchestrator_brief()
        # Helios's own MCP servers only (strict): the computer-use server, plus the
        # Composio Tool Router if it has been set up.
        mcp_configs = [str(conf.CONFIG_DIR / "mcp.json")]
        composio_cfg = conf.CONFIG_DIR / "composio_mcp.json"
        if composio_cfg.exists():
            mcp_configs.append(str(composio_cfg))
        # Headless Claude Code invocation. -p = print/non-interactive; stream-json +
        # include-partial-messages give us token-by-token deltas to forward to the UI;
        # strict-mcp-config limits tools to exactly our mcp_configs (no global servers);
        # permission-mode default routes tool approvals through our PreToolUse hook;
        # add-dir grants read/write into the Obsidian vault (conf.vault_path()).
        args = [
            conf.CLAUDE_BIN, "-p",
            "--model", route["model"],
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--append-system-prompt", system,
            "--mcp-config", *mcp_configs, "--strict-mcp-config",
            "--settings", str(conf.CONFIG_DIR / "claude_settings.json"),
            "--permission-mode", "default",
            "--add-dir", str(conf.vault_path()),
            "--plugin-dir", str(conf.ROOT / "agent_skills"),  # Helios's own SKILL.md skills (plan, debugging)
            # Guardrails: cap agentic turns (runaway-loop backstop — generous; a long GUI task can
            # legitimately use many) and degrade to a still-capable tier if the primary is overloaded.
            "--max-turns", str(int(conf.SETTINGS.get("claude", {}).get("max_turns", 80))),
            "--fallback-model", router.fallback_for(route["model"]),
        ]
        if resume_id:
            args += ["--resume", resume_id]   # continue the existing Claude session

        conf.log("brain", f"turn model={route['model']} resume={resume_id} interactive={interactive}")
        env = conf.claude_env()   # inherits env; adds ANTHROPIC_API_KEY only if configured
        if perm_sink:
            # Tell the PreToolUse hook where this turn's permission asks should surface (e.g. the
            # Telegram chat that started the turn). The hook (a child of this claude -p) inherits it.
            env = dict(env)
            env["HELIOS_PERM_SINK"] = perm_sink
        proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=CREATE_NO_WINDOW, cwd=str(conf.workspace_path()),
            env=env,
        )
        with self._proc_lock:   # publish atomically so panic/watchdog see a consistent handle
            self.proc = proc
        # Send the user message on stdin (keeps it off the argv length limit).
        try:
            self.proc.stdin.write(prompt + "\n")
            self.proc.stdin.close()
        except Exception:
            pass

        # Watchdog: hard-kill the process if a single turn exceeds turn_timeout seconds
        # (default 600 = 10 min) so a hung CLI can't wedge the assistant forever.
        timeout = int(conf.SETTINGS["claude"].get("turn_timeout", 600))
        watchdog = threading.Timer(timeout, self._kill)
        watchdog.start()
        # Drain stderr on a separate thread so a full stderr pipe can't deadlock the stdout
        # read loop; the buffer is inspected afterwards for the stale-session error.
        stderr_buf: list[str] = []
        threading.Thread(target=self._drain_stderr, args=(stderr_buf,), daemon=True).start()

        streamed: list[str] = []
        final_text = ""
        seen_tools: set[str] = set()
        meta: dict = {"tools": [], "usage": {}}   # ordered tool names + token/cost from the result
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                final_text = self._handle_event(obj, streamed, seen_tools, sess, meta) or final_text
        finally:
            watchdog.cancel()
            self.proc.wait()

        if interactive:
            self.session_id = sess["id"]
        elif session_hold is not None and sess["id"]:
            session_hold["id"] = sess["id"]   # per-caller continuity (e.g. a Telegram chat)
        if not final_text:
            final_text = "".join(streamed)
        # recover from a stale --resume session id by starting fresh (once). Detect it from
        # the error channel (or an error-only result), NOT from Helios's own reply text —
        # otherwise a reply that merely mentions the phrase would trigger a needless re-run.
        err_blob = "".join(stderr_buf).lower()
        stale = ("no conversation found" in err_blob
                 or (not streamed and final_text and "no conversation found" in final_text.lower()))
        if used_resume and not _retry and stale and not streamed:
            # Only retry when NOTHING was streamed to the UI yet — otherwise a fresh run would
            # re-emit token deltas and the dashboard would show the partial answer twice.
            conf.log("brain", "stale session id; retrying with a fresh session")
            if interactive:
                self.session_id = None
            if session_hold is not None:
                session_hold["id"] = None   # the held session is stale too — retry starts fresh
            with self._proc_lock:  # old proc already waited; clear before the retry reassigns
                self.proc = None
            return self._turn(message, record, _retry=True, interactive=interactive,
                              perm_sink=perm_sink, session_hold=session_hold)
        if not final_text and stderr_buf:
            self.emit("error", "".join(stderr_buf)[-500:].strip() or "No response.")
        # Per-turn insight for the dashboard: model, tools used (timeline), tokens/cost/duration.
        # Emitted before 'done' so the dashboard can attach it to the still-current reply bubble.
        if interactive:
            u = meta.get("usage", {})
            self.emit("usage", {"model": route["model"], "tier": route["tier"],
                                "tools": meta.get("tools", []), **u})
        self.emit("done", {"text": final_text})

        # If the user panicked mid-turn, don't persist the aborted exchange to memory or history.
        if self._aborted:
            return final_text
        # Background memory write-back (cheap model; never blocks the reply). Only for
        # interactive turns — automated turns must not write spurious facts to the vault.
        if interactive and final_text:
            light = conf.router_cfg().get("light", "haiku")
            threading.Thread(target=self._writeback,
                             args=(message, final_text, light), daemon=True).start()
            # Procedural memory: if this turn drove the PC, remember the action recipe so a
            # similar future task can reuse the approach (recalled in memory.build_digest).
            _tl = meta.get("tools", [])
            if any(str(t).startswith("mcp__computer__") for t in _tl):
                threading.Thread(target=memory.save_recipe, args=(message, _tl), daemon=True).start()
        if record and interactive and sess["id"]:
            try:
                db.upsert_conversation(sess["id"], title=message.strip()[:60])
                db.add_message(sess["id"], "user", message)
                db.add_message(sess["id"], "assistant", final_text or "")
            except Exception as e:  # pragma: no cover
                conf.log("brain", f"history persist failed: {e}")
        return final_text

    def _handle_event(self, obj: dict, streamed: list[str], seen_tools: set[str],
                      sess: dict, meta: dict) -> str | None:
        """Dispatch one parsed stream-json event from the CLI.

        Captures the session id (from the init + final result events), forwards text deltas
        as 'token' events, announces tool_use blocks, and returns the final result string
        when the terminal 'result' event arrives (otherwise None). `streamed` accumulates
        text deltas as a fallback if no final result is provided; `seen_tools` dedupes tool
        announcements by block id; `sess` is the per-turn session holder; `meta` accumulates the
        ordered tool list + the usage/cost from the result event (for the dashboard)."""
        t = obj.get("type")
        if t == "system" and obj.get("subtype") == "init":
            sess["id"] = obj.get("session_id") or sess["id"]
        elif t == "stream_event":
            ev = obj.get("event", {})
            et = ev.get("type")
            if et == "content_block_delta":
                delta = ev.get("delta", {})
                if delta.get("type") == "text_delta" and delta.get("text"):
                    streamed.append(delta["text"])
                    self.emit("token", delta["text"])
            elif et == "content_block_start":
                blk = ev.get("content_block", {})
                if blk.get("type") == "tool_use":
                    self._announce_tool(blk, seen_tools, meta)
        elif t == "assistant":
            for blk in obj.get("message", {}).get("content", []):
                if blk.get("type") == "tool_use":
                    self._announce_tool(blk, seen_tools, meta)
        elif t == "result":
            sess["id"] = obj.get("session_id") or sess["id"]
            usage = obj.get("usage") or {}
            meta["usage"] = {
                "in": (usage.get("input_tokens", 0) or 0)
                + (usage.get("cache_read_input_tokens", 0) or 0)
                + (usage.get("cache_creation_input_tokens", 0) or 0),
                "out": usage.get("output_tokens", 0) or 0,
                "cost": obj.get("total_cost_usd"),
                "ms": obj.get("duration_ms") or obj.get("duration_api_ms"),
            }
            return obj.get("result") or None
        return None

    def _announce_tool(self, blk: dict, seen_tools: set[str], meta: dict | None = None) -> None:
        """Emit a 'tool' UI event for a tool_use block, once per block id, and record its name in
        meta['tools'] (ordered, for the per-turn timeline).

        A tool can surface in both the streamed partial messages and the final assistant
        message, so seen_tools dedupes by block id."""
        bid = blk.get("id", "")
        if bid in seen_tools:
            return
        seen_tools.add(bid)
        name = blk.get("name", "tool")
        if meta is not None:
            meta.setdefault("tools", []).append(name)
        self.emit("tool", {"name": name})

    def _writeback(self, message: str, reply: str, model: str) -> None:
        """Background memory extraction (runs on its own thread, off the reply path).
        Uses a cheap model to distill durable facts from this turn into the Obsidian vault
        (memory.extract_and_write) and reports a 'memory' status to the UI."""
        try:
            status = memory.extract_and_write(message, reply, model)
            self.emit("memory", status)
        except Exception as e:  # pragma: no cover
            conf.log("brain", f"writeback error: {e}")

    def _drain_stderr(self, buf: list[str]) -> None:
        """Consume the CLI's stderr line-by-line into `buf` and log each line. Runs on a
        daemon thread so stderr can't fill its pipe and block the stdout reader."""
        try:
            if self.proc and self.proc.stderr:
                for line in self.proc.stderr:
                    buf.append(line)
                    conf.log("brain.stderr", line.rstrip())
        except Exception:
            pass

    def _kill(self) -> None:
        """Forcibly terminate the running claude -p process and its whole child tree.

        Windows: taskkill /T kills the entire process tree, which matters because the
        venv pythonw stub spawns a base-python child AND the CLI itself spawns subprocesses
        — a plain p.kill() would orphan those. Falls back to p.kill() if taskkill fails.
        Invoked by both the watchdog timeout and panic() (other threads), so snapshot self.proc
        under the lock to get a consistent handle even as a turn assigns/clears it."""
        with self._proc_lock:
            p = self.proc
        if p and p.poll() is None:   # only if a process exists and hasn't already exited
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                               capture_output=True, creationflags=CREATE_NO_WINDOW)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
