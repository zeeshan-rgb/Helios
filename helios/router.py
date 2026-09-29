"""Automatic per-task model routing.

Picks a Claude tier (light / medium / heavy) for each turn so quick things run on a
cheap fast model and hard things run on the strong one. Heuristics-first (zero added
latency); an optional light-model triage refines the ambiguous middle band.

Tiers map to model aliases via conf.router_cfg() (defaults: light=haiku, medium=sonnet,
heavy=opus); brain.py feeds the chosen model to claude_cli / the main turn. Routing can
be turned off (cfg.auto=False) to pin a single fixed model, and the user can force a
tier per message with a leading /haiku /sonnet /opus /fable override.

Related: claude_cli (runs the optional triage call), conf (router settings), brain.py.
"""

from __future__ import annotations

import re

from . import conf, llm

# Words that signal genuine engineering / multi-step / diagnostic work -> heavy. Deliberately
# NARROW (2026-07: opus is the escalation tier, sonnet the workhorse): pure Q&A verbs that the
# medium model handles fine (explain/compare/research/analyze/strategy/figure out) route medium —
# faster replies, and opus is saved for the genuinely hard stuff.
_HEAVY = re.compile(
    r"\b(debug|architect|design|refactor|investigat|optimi[sz]e|"
    r"implement|algorithm|trade[- ]?off|root cause|why\b|"
    r"plan\b|multi-?step|complex|across (the|my|several)|"
    r"end[- ]to[- ]end|step by step|think (hard|carefully))\b",
    re.I,
)
# Quick, low-stakes patterns -> light.
_LIGHT = re.compile(
    r"^(hi|hey|hello|yo|thanks|thank you|ok|okay|cool|nice|good (morning|night)|"
    r"what time|what'?s the time|what day|open |launch |start |close |play |pause |"
    r"mute|unmute|volume|next track|screenshot|who is |what is |whats |when is )",
    re.I,
)
# Screen/GUI interaction needs strong visual grounding -> never route to the weak tier.
_GUI = re.compile(
    r"\b(click|double[- ]?click|right[- ]?click|type|scroll|drag|cursor|mouse|keyboard|"
    r"button|screenshot|screen|window|tab|menu|press|select|open|launch|close|navigate|"
    r"go to|focus|paste|highlight|hover|toolbar|icon|dropdown|minimi[sz]e|maximi[sz]e|"
    r"headphone|speaker|audio|volume|mute|brightness|bluetooth|wi-?fi|webcam|microphone|"
    r"\bmic\b|device|restart|shut ?down|switch to|display|monitor|settings?)\b",
    re.I,
)
# App/integration tasks call tools and judgment — the weak tier hallucinates procedures.
_INTEGRATION = re.compile(
    r"\b(gmail|e-?mails?|inbox|compose|drafts?|calendars?|schedule|meetings?|github|"
    r"repos?|repositor\w*|issues?|pull request|notion|drive|docs?|spreadsheets?|"
    r"sheets?|send|connect\w*|composio|integrations?)\b",
    re.I,
)
# Leading manual override token, e.g. "/opus refactor this" — forces a tier, bypassing heuristics.
_OVERRIDE = re.compile(r"^/(haiku|sonnet|opus|fable)\b\s*", re.I)
# Which tier each override alias maps to (fable is a heavy-class model).
_TIER_FOR_ALIAS = {"haiku": "light", "sonnet": "medium", "opus": "heavy", "fable": "heavy"}


def _models() -> dict:
    """Resolve the tier->model-alias mapping from settings, with built-in defaults."""
    cfg = conf.router_cfg()
    return {
        "light": cfg.get("light", "haiku"),
        "medium": cfg.get("medium", "sonnet"),
        "heavy": cfg.get("heavy", "opus"),
    }


def fallback_for(model: str) -> str:
    """The --fallback-model for a chosen primary: the medium model, unless the primary IS the
    medium model (then light). Fallback fires only when the primary is overloaded/unavailable,
    so a turn degrades to a still-capable tier instead of erroring out."""
    models = _models()
    return models["light"] if model == models["medium"] else models["medium"]


def _heuristic(message: str) -> tuple[str, float, str]:
    """Cheap, regex/length-based first guess. Return (tier, confidence 0..1, reason).

    Signals of real work (heavy keywords, long >400 chars, code fences, 4+ lines) -> heavy.
    Short + light-pattern, or very short (<=40 chars) -> light. Everything else falls
    through to medium with low confidence — which is the band the optional triage targets.
    """
    msg = message.strip()
    n = len(msg)
    # Long / code-bearing / multi-line messages almost always need real reasoning.
    if _HEAVY.search(msg) or n > 400 or "```" in msg or msg.count("\n") >= 4:
        return "heavy", 0.85, "complex/multi-step request"
    if n <= 90 and _LIGHT.search(msg):
        return "light", 0.85, "short, low-stakes request"
    if n <= 40:
        return "light", 0.7, "very short request"
    return "medium", 0.5, "normal task"   # ambiguous middle band (low confidence)


def choose_model(message: str) -> dict:
    """Decide which model handles this turn. Return {model, tier, reason, message}.

    `message` in the result is the text the brain should actually use — identical to the
    input except when a manual /override prefix was stripped off. Resolution order:
      1. Manual /haiku|/sonnet|/opus|/fable override (wins outright).
      2. Auto routing off -> the single fixed model from settings.
      3. Heuristic guess, then GUI/integration safety lift off the weak tier, then
         (if enabled) light-model triage for the low-confidence medium band.
    """
    models = _models()
    cfg = conf.router_cfg()

    # Manual override: leading /haiku /sonnet /opus /fable
    m = _OVERRIDE.match(message)
    if m:
        alias = m.group(1).lower()
        tier = _TIER_FOR_ALIAS[alias]
        cleaned = message[m.end():]
        return {"model": alias, "tier": tier, "reason": "manual override", "message": cleaned}

    # Auto routing disabled -> always use the user's single pinned model.
    if not cfg.get("auto", True):
        fixed = conf.SETTINGS["claude"]["model"]
        return {"model": fixed, "tier": "fixed", "reason": "router off", "message": message}

    tier, conf_score, reason = _heuristic(message)

    # Screen control + app/tool tasks misbehave on the weak tier — lift to medium.
    if tier == "light" and _GUI.search(message):
        tier, reason = "medium", "screen interaction needs stronger visual grounding"
    elif tier == "light" and _INTEGRATION.search(message):
        tier, reason = "medium", "app/integration task needs a stronger model"

    # Optional model triage only for the genuinely ambiguous middle band.
    # Gated on conf_score < 0.6 so we only pay the extra latency when the heuristic is unsure.
    if cfg.get("triage", False) and tier == "medium" and conf_score < 0.6:
        verdict = _triage(message, models["light"])
        if verdict:
            tier, reason = verdict["tier"], "triage: " + verdict.get("reason", "")[:80]

    return {"model": models[tier], "tier": tier, "reason": reason, "message": message}


def _triage(message: str, light_model: str) -> dict | None:
    """Ask the light model to classify the request's needed tier. Return {tier,reason} or None.

    A cheap tie-breaker for the ambiguous medium band: one tool-less claude_cli call on
    the light model. Returns None on failure or an out-of-range tier so the caller keeps
    its heuristic verdict.
    """
    schema = {
        "type": "object",
        "properties": {
            "tier": {"type": "string", "enum": ["light", "medium", "heavy"]},
            "reason": {"type": "string"},
        },
        "required": ["tier", "reason"],
    }
    system = ("You triage how much AI capability a request needs. "
              "light=trivial/lookup, medium=normal task, heavy=complex reasoning or multi-step. "
              "Output ONLY one minified JSON object: {\"tier\":\"...\",\"reason\":\"...\"}.")
    res = llm.complete_json(f"Request: {message!r}", model=light_model,
                            schema=schema, system=system, timeout=40)
    if res and res.get("data") and res["data"].get("tier") in ("light", "medium", "heavy"):
        return res["data"]
    return None
