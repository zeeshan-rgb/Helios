"""Provider-agnostic LLM access for the tiered brain.

Two consumers depend on this:
  - Non-streaming JSON/text helper calls — memory.extract_and_write and router._triage call
    complete_json() here instead of claude_cli directly, so they work on a machine that has NO
    Claude CLI (the lite engine).
  - The lite brain (lite_brain.py) builds an OpenAI-compatible client via make_client() and
    drives chat.completions itself (streaming + tool-calling).

Routing: when conf.brain_engine() == "claude" the helper calls delegate to claude_cli (the FREE
Claude-Code path — never swap that for a direct api.anthropic.com call). Otherwise we use the
OpenAI Python SDK pointed at the configured provider's OpenAI-compatible endpoint
(openai / openrouter / gemini / ollama / groq). The `openai` package is imported LAZILY inside
make_client() so a Claude-only install doesn't need it.
"""

from __future__ import annotations

import json
import os

from . import claude_cli, conf

# Default OpenAI-compatible base URLs per provider (overridable via secrets.toml [<provider>].base_url).
_PROVIDER_BASE = {
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "ollama": "http://127.0.0.1:11434/v1",
}
# Sensible tool-capable default model per provider when [brain].model is left blank.
_PROVIDER_DEFAULT_MODEL = {
    "openai": "gpt-4o",
    "groq": "llama-3.3-70b-versatile",
    "openrouter": "openai/gpt-4o",
    "gemini": "gemini-3.8-flash",
    "ollama": "llama3.1",
}
# Claude tier aliases the router/memory may pass; meaningless to a lite provider, so we
# substitute the configured lite model if one of these leaks into the lite path.
_CLAUDE_ALIASES = {"haiku", "sonnet", "opus", "fable"}


def provider() -> str:
    """The configured lite provider id (openai/openrouter/gemini/ollama/groq)."""
    return str(conf.brain_cfg().get("provider") or "openai").strip().lower()


def lite_model() -> str:
    """The model the lite brain should use: [brain].model, else a per-provider default."""
    m = (conf.brain_cfg().get("model") or "").strip()
    return m or _PROVIDER_DEFAULT_MODEL.get(provider(), "gpt-4o")


def _resolve_model(model: str | None) -> str:
    """Map a requested model to one valid for the current lite provider. Claude tier aliases
    (or empty) fall back to the configured lite model."""
    if not model or str(model).lower() in _CLAUDE_ALIASES or str(model).lower().startswith("claude"):
        return lite_model()
    return str(model)


def make_client(prov: str | None = None):
    """Build an OpenAI-compatible client for `prov` (default: the configured lite provider).

    base_url + api_key come from secrets.toml [<prov>] (merged into settings by conf.load), with
    built-in base-URL defaults. Ollama is keyless (a placeholder key is sent and ignored).
    Raises ImportError if the `openai` package isn't installed (lite-only dependency)."""
    prov = (prov or provider()).lower()
    pc = conf.provider_cfg(prov)
    base_url = pc.get("base_url") or _PROVIDER_BASE.get(prov) or _PROVIDER_BASE["openai"]
    api_key = (pc.get("api_key") or os.environ.get(f"{prov.upper()}_API_KEY")
               or ("ollama" if prov == "ollama" else None))
    from openai import OpenAI  # lazy: only needed for the lite engine
    return OpenAI(base_url=base_url, api_key=api_key or "missing-api-key")


def complete_json(prompt: str, *, model: str, schema: dict | None = None,
                  system: str | None = None, timeout: int = 120) -> dict | None:
    """Engine-routed JSON/text completion. Same return shape as claude_cli.complete_json:
    {text, data, session_id, cost, error}. Returns None on hard failure."""
    if conf.brain_engine() == "claude":
        return claude_cli.complete_json(prompt, model=model, schema=schema,
                                        system=system, timeout=timeout)
    if conf.brain_engine() == "gemini":
        from . import gemini_cli
        return gemini_cli.complete_json(prompt, model=model, schema=schema,
                                        system=system, timeout=timeout)
    if conf.brain_engine() == "antigravity":
        from . import agy_cli
        return agy_cli.complete_json(prompt, model=model, schema=schema,
                                     system=system, timeout=timeout)
    return _lite_complete_json(prompt, model=_resolve_model(model), schema=schema,
                               system=system, timeout=timeout)


def _lite_complete_json(prompt: str, *, model: str, schema: dict | None,
                        system: str | None, timeout: int) -> dict | None:
    """One non-streaming OpenAI-compatible completion. When a schema is given we ask for JSON
    (response_format json_object) and parse it tolerantly, retrying without response_format for
    providers that reject it (e.g. some Ollama models)."""
    try:
        client = make_client()
    except Exception as e:
        conf.log("llm", f"client init failed: {e}")
        return None
    sys_prompt = (system or "").strip()
    if schema:
        sys_prompt = (sys_prompt + "\n\nReturn ONLY a single minified JSON object — "
                      "no prose, no code fences.").strip()
    messages = []
    if sys_prompt:
        messages.append({"role": "system", "content": sys_prompt})
    messages.append({"role": "user", "content": prompt})
    base_kwargs = dict(model=model, messages=messages, timeout=timeout, temperature=0)
    try:
        if schema:
            try:
                resp = client.chat.completions.create(
                    response_format={"type": "json_object"}, **base_kwargs)
            except Exception as e:
                conf.log("llm", f"json_object rejected, retrying plain: {e}")
                resp = client.chat.completions.create(**base_kwargs)
        else:
            resp = client.chat.completions.create(**base_kwargs)
    except Exception as e:
        conf.log("llm", f"lite complete failed: {e}")
        return None
    text = ""
    try:
        text = (resp.choices[0].message.content or "") if resp.choices else ""
    except Exception:
        pass
    data = None
    if schema and text:
        try:
            data = json.loads(claude_cli._strip_fences(text))
        except Exception:
            data = claude_cli._extract_json(text)
    cost = None
    try:
        usage = getattr(resp, "usage", None)
        if usage is not None:
            cost = None  # most OpenAI-compatible providers don't return a dollar cost
    except Exception:
        pass
    return {"text": text, "data": data, "session_id": None, "cost": cost, "error": None}
