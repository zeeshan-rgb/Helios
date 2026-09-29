"""Thin wrapper around the Claude Code CLI for non-streaming, tool-less helper calls
(model triage + memory extraction). The full conversational turn lives in brain.py.

The engine is the official `claude` CLI (`claude -p`), which is FREE on the user's
subscription. Do NOT swap this for a direct api.anthropic.com call — that path is gated
to paid usage. Helpers here run --no-session-persistence and --tools "" so they're
stateless, side-effect-free one-shots that just return parsed JSON.

Callers: router._triage and memory.extract_and_write (both via complete_json).
"""

from __future__ import annotations

import json
import subprocess

from . import conf

CREATE_NO_WINDOW = 0x08000000  # Windows flag: never flash a console window when spawning the CLI


def _extract_json(text: str) -> dict | None:
    """Find and parse the first balanced {...} JSON object in arbitrary model text.

    Fallback for when the model wraps its JSON in prose. Scans for a top-level '{',
    tracks brace depth while respecting string literals/escapes (so braces inside
    strings don't throw off the count), and parses the first balanced span that loads
    cleanly. Tries the next '{' if a candidate fails to parse. Returns None if none work.
    """
    start = text.find("{")
    while start != -1:
        # Track brace nesting, but only outside of string literals.
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i + 1])
                        except Exception:
                            break
        start = text.find("{", start + 1)
    return None


def _strip_fences(text: str) -> str:
    """Remove a surrounding ```...``` Markdown code fence (and a leading 'json' tag).

    Models often return JSON wrapped in a fenced block; this unwraps it so json.loads
    can parse the body directly. Returns text unchanged if there's no opening fence.
    """
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t
        if t.endswith("```"):
            t = t[: -3]
        # drop a leading 'json' language tag if present
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    return t.strip()


def complete_json(prompt: str, *, model: str, schema: dict | None = None,
                  system: str | None = None, timeout: int = 120) -> dict | None:
    """Run a single, tool-less `claude -p` call and return a parsed result.

    Args:
        prompt: the user prompt sent to the CLI.
        model: model alias (haiku/sonnet/opus/...) — chosen by router for the task.
        schema: optional JSON schema; when given the CLI is asked to emit JSON and the
            response text is parsed into the returned "data" field.
        system: optional system prompt.
        timeout: hard subprocess timeout in seconds.

    Returns dict: {text, data (parsed JSON if schema), session_id, cost, error}.
    Returns None on hard failure (timeout, spawn error, or empty/unparseable envelope).
    The CLI's outer JSON envelope (--output-format json) is parsed first; for schema
    calls the inner result text is then parsed via _strip_fences, falling back to
    _extract_json. Stays on the free CLI engine — see module docstring.
    """
    # The prompt goes on STDIN, not argv: a long memory-extraction prompt can exceed Windows'
    # ~32 KB command-line limit, which made the spawn fail and silently dropped vault write-back
    # on the richest conversations. Bare `claude -p` reads the prompt from stdin (same as brain.py).
    args = [conf.CLAUDE_BIN, "-p",
            "--output-format", "json",       # machine-readable envelope w/ result + metadata
            "--model", model,
            "--no-session-persistence",      # stateless: don't pollute the user's session history
            "--tools", ""]                   # tool-less: pure text/JSON completion, no side effects
    if system:
        args += ["--system-prompt", system]
    if schema:
        args += ["--json-schema", json.dumps(schema)]
    try:
        proc = subprocess.run(args, input=prompt, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace",
                              creationflags=CREATE_NO_WINDOW, env=conf.claude_env())
    except subprocess.TimeoutExpired:
        conf.log("claude_cli", f"timeout model={model}")
        return None
    except Exception as e:  # pragma: no cover
        conf.log("claude_cli", f"spawn error: {e}")
        return None

    out = (proc.stdout or "").strip()
    if not out:
        conf.log("claude_cli", f"empty stdout rc={proc.returncode} err={proc.stderr[:300]}")
        return None
    try:
        env = json.loads(out)   # outer CLI envelope (result text + session_id/cost/is_error)
    except json.JSONDecodeError:
        conf.log("claude_cli", f"non-json stdout: {out[:300]}")
        return {"text": out, "data": None, "error": "non-json-envelope"}

    text = env.get("result", "") if isinstance(env, dict) else str(env)
    data = None
    if schema and text:
        # Parse the model's JSON answer: try the fenced/clean path first, then the
        # tolerant brace-scanner if the model added surrounding prose.
        try:
            data = json.loads(_strip_fences(text))
        except Exception:
            data = _extract_json(text)
        if data is None:
            conf.log("claude_cli", f"json parse fail: {text[:200]}")
    return {
        "text": text,
        "data": data,
        "session_id": env.get("session_id") if isinstance(env, dict) else None,
        "cost": env.get("total_cost_usd") if isinstance(env, dict) else None,
        "error": env.get("is_error") if isinstance(env, dict) else None,
    }
