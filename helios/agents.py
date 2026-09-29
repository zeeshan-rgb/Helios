"""Specialist side-agent roster + routing.

The orchestrator (the live brain) hands substantial or specialized work off to the
best-suited side agent. Each specialist is the SAME Helios persona plus a focus note, so it
keeps Helios's identity and full tool access — it's just pointed at what it's best at.

Routing is either explicit (the orchestrator names the agent via delegate_task) or "auto"
(the quick keyword heuristic here). Keep this module dependency-light: it's imported by both
the app and the MCP server process.
"""

from __future__ import annotations

import re

from . import conf

GENERALIST = "generalist"
SUPERVISOR = "supervisor"

# The mission supervisor coordinates a team; it plans + delegates rather than doing the work.
SUPERVISOR_NOTE = (
    "You are the MISSION SUPERVISOR for a team of side agents. You do NOT do the hands-on work "
    "yourself — you PLAN and COORDINATE. Work the mission like this:\n"
    "1. Break the goal into the SMALL number of concrete subtasks it truly needs.\n"
    "2. For each subtask, spawn the best agent with spawn_agent(task, role): use a preset role "
    "(researcher, operator, coder, organizer, writer) when one fits, or just describe what you "
    "need in the task for a custom helper. Spawn INDEPENDENT subtasks in parallel "
    "(spawn_agent(task, role, wait=false), then wait_for_agent on each); run DEPENDENT steps in "
    "sequence (spawn_agent(task, role, wait=true) so you get the result before the next step).\n"
    "3. Use read_mission to gather what your agents reported on the shared blackboard, and "
    "post_finding to leave them notes or hand one agent's output to the next.\n"
    "4. When the goal is met, call mission_result with a clear, synthesized final answer for the user.\n"
    "Keep the team lean — never spawn more agents than the task needs."
)

# name -> {when (orchestrator blurb), verbs (the action, weighted higher), kw (context nouns),
#          note (persona focus)}. Auto-routing scores 3*verb-hits + 1*context-hits so the verb
# ("draft", "tidy", "click") drives the choice over incidental nouns ("email", "file").
SPECIALISTS: dict[str, dict] = {
    "researcher": {
        "when": "web research; finding facts, news, prices; comparing options; summarizing sources",
        "verbs": r"research|look ?up|find out|\bsearch\b|google|compare|\bvs\.?\b",
        "kw": r"news|price|cost of|article|paper|sources?|documentation|\bdocs\b|who is|what is|latest",
        "note": ("You are Helios's RESEARCH specialist. Dig up accurate, current information and "
                 "synthesize it clearly. Prefer web search/fetch and the deep_research tool; "
                 "cross-check key claims and note your sources. Avoid driving the GUI unless it's "
                 "truly required."),
    },
    "operator": {
        "when": "hands-on PC control: clicking, typing, navigating apps/windows, filling forms, GUI automation",
        "verbs": r"click|\btype\b|scroll|\bdrag\b|navigat|automat|screenshot|\bpress\b|paste|select|focus",
        "kw": r"button|window|menu|\btab\b|\bform\b|mouse|keyboard|browser|\bapp\b|\bscreen\b|cursor",
        "note": ("You are Helios's COMPUTER OPERATOR specialist. You drive the mouse and keyboard "
                 "and read the screen to get things done in real apps. Screenshot first, act "
                 "deliberately, and verify the result. Respect the screen lock and defer if the user is "
                 "actively working."),
    },
    "coder": {
        "when": "writing/editing/running code, scripts, configs; builds; tests; debugging",
        "verbs": r"\bcode\b|debug|refactor|compile|implement|\bprogram\b|run the",
        "kw": r"python|javascript|typescript|script|function|\bclass\b|\bbug\b|\brepo\b|\bgit\b|"
              r"\bapi\b|\bnpm\b|\bpip\b|tests?\b|\bbuild\b",
        "note": ("You are Helios's CODING specialist. Read before you write, match the existing "
                 "style, make focused edits, and run/verify your changes. Use Read/Write/Edit and "
                 "the shell."),
    },
    "organizer": {
        "when": "files & folders, tidying Downloads, moving/renaming, reminders, scheduling, calendar, email triage",
        "verbs": r"organi[sz]e|tidy|clean ?up|\bsort\b|rename|\bmove\b|archive|schedule|remind",
        "kw": r"\bfile\b|folder|download|reminder|calendar|meeting|\bemail\b|inbox",
        "note": ("You are Helios's ORGANIZER specialist. Handle files, scheduling, calendar and "
                 "email tidily and safely. Be cautious with destructive file operations — confirm "
                 "what you're removing and prefer moving to a folder over deleting."),
    },
    "writer": {
        "when": "drafting or polishing text: documents, emails, messages, notes, creative writing",
        "verbs": r"\bwrite\b|draft|compose|rephrase|reword|proofread|summari[sz]e",
        "kw": r"essay|document\b|letter|\bnote\b|\bblog\b|\bpost\b|caption|\bemail\b|reply",
        "note": ("You are Helios's WRITING specialist. Produce clear, well-structured prose in "
                 "the user's voice. Match the requested tone and length; tighten and proofread before "
                 "finishing."),
    },
}

NAMES = list(SPECIALISTS.keys())


def normalize(agent: str | None) -> str:
    """Map any input to a known specialist, 'auto', or GENERALIST (unknown -> 'auto')."""
    a = (agent or "auto").strip().lower()
    if a in SPECIALISTS or a in ("auto", GENERALIST):
        return a
    return "auto"


def route(task: str) -> str:
    """Heuristic pick of the best specialist for a task; GENERALIST if nothing clearly fits.

    The action verb is weighted 3x context nouns, so 'draft an email' -> writer (not organizer).
    """
    t = task or ""
    best, best_score = GENERALIST, 0
    for name, spec in SPECIALISTS.items():
        score = 3 * len(re.findall(spec["verbs"], t, re.I)) + len(re.findall(spec["kw"], t, re.I))
        if score > best_score:
            best, best_score = name, score
    return best


def resolve(agent: str | None, task: str) -> str:
    """Turn an agent request ('auto'/name/None) into a concrete specialist or GENERALIST."""
    a = normalize(agent)
    if a == "auto":
        return route(task)
    return a


_base_cache: str | None = None


def _base() -> str:
    """Load (and cache) the shared base Helios persona from persona_helios.md.

    Read once and memoized in _base_cache — every specialist persona is this base
    plus a focus note (see persona_for). Falls back to a one-line identity if the
    file is missing so routing/personas never hard-fail.
    """
    global _base_cache
    if _base_cache is None:
        try:
            _base_cache = (conf.CONFIG_DIR / "persona_helios.md").read_text(encoding="utf-8")
        except Exception:
            _base_cache = "You are Helios, the user's local AI assistant. Address the user as \"sir\"."
    return _base_cache


def persona_for(agent: str | None, custom_note: str | None = None) -> str:
    """Full system persona for a role: base Helios + a focus note.

    custom_note (free text) defines an ad-hoc role for a mission worker; otherwise `agent`
    selects the supervisor, a preset specialist, or the plain generalist.
    """
    if custom_note:
        return _base() + "\n\n## Your role on this mission\n" + custom_note.strip()
    a = (agent or "").strip().lower()
    if a == SUPERVISOR:
        return _base() + "\n\n" + SUPERVISOR_NOTE
    spec = SPECIALISTS.get(normalize(agent))
    if not spec:
        return _base()  # generalist / auto / unknown -> plain Helios
    return _base() + "\n\n## Your focus right now\n" + spec["note"]


def orchestrator_brief() -> str:
    """Roster + delegation guidance injected into the ORCHESTRATOR's system prompt only."""
    lines = [
        "## Your specialist side agents",
        "You are the ORCHESTRATOR. For substantial, slow, or specialized work, hand it off to the "
        "best-suited specialist side agent with the `delegate_task` tool instead of doing "
        "everything yourself — they run in the background in parallel, so you stay responsive to "
        "the user. Available agents:",
    ]
    lines += [f"- {name} — {spec['when']}" for name, spec in SPECIALISTS.items()]
    lines.append(
        'Pass the single best agent name (or "auto" to let Helios choose). Delegate a single task '
        "when it's slow or clearly fits one specialist; for quick things, just answer directly. "
        "After delegating, tell the user you've handed it off and to whom.")
    lines.append(
        "For a COMPLEX, multi-part task that needs SEVERAL agents working together — or several "
        "dependent steps (e.g. 'research the best X and then open the top results in Chrome') — "
        "use `start_mission(goal)` instead. A supervisor agent will break it down, spawn a team "
        "that collaborates on a shared blackboard, and report back. Use delegate_task for one "
        "hand-off; start_mission when it takes a team.")
    return "\n".join(lines)
