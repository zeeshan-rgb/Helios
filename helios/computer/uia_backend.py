"""Read-only Windows UI Automation reader (the `uiautomation` package, in-process).

Never clicks or types — actions belong to cua-driver. Every walk is bounded (depth, element
count, time), filtered to controls worth showing a model (named / actionable — UFO²'s "control
filtering"), and passed through policy_bridge (no password values, no password-manager windows,
secrets redacted).
"""

from __future__ import annotations

import contextlib
import re
import time

from . import policy_bridge
from .interface import Element, ScreenContext, Window

# Roles worth showing (UIA ControlTypeName without "Control"). Unnamed panes/groups are noise.
_ACTIONABLE = {"Button", "SplitButton", "Edit", "ComboBox", "CheckBox", "RadioButton",
               "Hyperlink", "MenuItem", "ListItem", "TabItem", "TreeItem", "DataItem",
               "Slider", "Spinner", "Document", "HeaderItem", "Menu"}
_TEXTUAL = {"Text", "Document", "Edit"}
_SKIP_ALWAYS = {"ScrollBar", "Thumb", "TitleBar", "Separator"}

_pnames: dict[int, str] = {}


def _auto():
    import uiautomation as auto
    return auto


def _com():
    """COM must be initialised per thread for UIA."""
    try:
        return _auto().UIAutomationInitializerInThread()
    except Exception:  # pragma: no cover
        return contextlib.nullcontext()


# ------------------------------------------------------------------ small accessors (patchable)

def _foreground():
    c = _auto().GetForegroundControl()
    return c.GetTopLevelControl() if c else None


def _from_handle(hwnd: int):
    return _auto().ControlFromHandle(int(hwnd))


def _top_windows():
    return _auto().GetRootControl().GetChildren()


def _focused():
    return _auto().GetFocusedControl()


def _walk(ctrl, max_depth: int):
    return _auto().WalkControl(ctrl, includeTop=False, maxDepth=max_depth)


def _pname(pid: int) -> str:
    if pid not in _pnames:
        try:
            import psutil
            _pnames[pid] = psutil.Process(pid).name()
        except Exception:
            _pnames[pid] = ""
    return _pnames[pid]


def _pattern(ctrl, name: str):
    try:
        return ctrl.GetPattern(getattr(_auto().PatternId, name))
    except Exception:
        return None


def _role(ctrl) -> str:
    return (getattr(ctrl, "ControlTypeName", "") or "").removesuffix("Control")


def _human(role: str) -> str:
    return re.sub(r"(?<!^)([A-Z])", r" \1", role).lower()


def _rect(ctrl) -> tuple[int, int, int, int]:
    try:
        r = ctrl.BoundingRectangle
        return (int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top))
    except Exception:
        return (0, 0, 0, 0)


def _get(ctrl, attr, default=None):
    try:
        return getattr(ctrl, attr)
    except Exception:
        return default


# ------------------------------------------------------------------ windows

def to_window(ctrl, foreground: bool = False) -> Window:
    pid = int(_get(ctrl, "ProcessId", 0) or 0)
    app = _pname(pid)
    title = policy_bridge.clean(_get(ctrl, "Name", "") or "", 200)
    return Window(hwnd=int(_get(ctrl, "NativeWindowHandle", 0) or 0), pid=pid, app=app,
                  title=title, class_name=_get(ctrl, "ClassName", "") or "", rect=_rect(ctrl),
                  foreground=foreground, sensitive=policy_bridge.sensitive_reason(app, title))


def list_windows(limit: int = 15) -> list[Window]:
    """Visible top-level windows, foreground first."""
    with _com():
        fg = _foreground()
        fg_h = int(_get(fg, "NativeWindowHandle", 0) or 0) if fg else 0
        out = []
        for c in _top_windows():
            r = _rect(c)
            if r[2] <= 0 or r[3] <= 0 or _get(c, "IsOffscreen", False):
                continue
            title = _get(c, "Name", "") or ""
            if not title or title.startswith("Cua.AgentCursorOverlay"):
                continue
            h = int(_get(c, "NativeWindowHandle", 0) or 0)
            out.append(to_window(c, foreground=(h == fg_h)))
        out.sort(key=lambda w: not w.foreground)
        return out[:limit]


def find_window(title_or_app: str):
    """Top-level window control whose title or process name contains the text (foreground wins)."""
    q = (title_or_app or "").lower().strip()
    with _com():
        fg = _foreground()
        cands = ([fg] if fg else []) + list(_top_windows())
        for c in cands:
            if c is None:
                continue
            pid = int(_get(c, "ProcessId", 0) or 0)
            if q in (_get(c, "Name", "") or "").lower() or q in _pname(pid).lower():
                return c
    return None


# ------------------------------------------------------------------ elements

def to_element(ctrl, depth: int = 0) -> Element:
    role = _role(ctrl)
    name = policy_bridge.clean(_get(ctrl, "Name", "") or "", 160)
    value = ""
    if _get(ctrl, "IsPassword", False):
        value = "[hidden password field]"
    else:
        vp = _pattern(ctrl, "ValuePattern")
        if vp is not None:
            value = policy_bridge.clean(_get(vp, "Value", "") or "", 200)
    sel = _pattern(ctrl, "SelectionItemPattern")
    return Element(role=_human(role), name=name, value=value,
                   automation_id=policy_bridge.clean(_get(ctrl, "AutomationId", "") or "", 80),
                   enabled=bool(_get(ctrl, "IsEnabled", True)),
                   focused=bool(_get(ctrl, "HasKeyboardFocus", False)),
                   selected=bool(_get(sel, "IsSelected", False)) if sel is not None else False,
                   rect=_rect(ctrl), depth=depth)


def _keep(ctrl) -> bool:
    role = _role(ctrl)
    if role in _SKIP_ALWAYS or _get(ctrl, "IsOffscreen", False):
        return False
    named = bool((_get(ctrl, "Name", "") or "").strip())
    if role in _ACTIONABLE:
        return named or role in ("Edit", "Document", "ComboBox")
    return role == "Text" and named


def walk(window_ctrl, *, max_depth: int = 14, max_elements: int = 250,
         time_budget: float = 1.5) -> tuple[list[tuple[object, int]], bool]:
    """(kept controls with depth, truncated?) — bounded walk of one window."""
    kept, t0 = [], time.monotonic()
    for ctrl, depth in _walk(window_ctrl, max_depth):
        if time.monotonic() - t0 > time_budget or len(kept) >= max_elements:
            return kept, True
        if _keep(ctrl):
            kept.append((ctrl, depth))
    return kept, False


def _doc_text(ctrl, limit: int) -> str:
    tp = _pattern(ctrl, "TextPattern")
    if tp is None:
        return ""
    try:
        return tp.DocumentRange.GetText(limit) or ""
    except Exception:
        return ""


def _selection_text(ctrl, limit: int) -> str:
    tp = _pattern(ctrl, "TextPattern")
    if tp is None:
        return ""
    try:
        return " ".join((r.GetText(limit) or "") for r in tp.GetSelection())[:limit]
    except Exception:
        return ""


def _dialogs(window_ctrl, kept) -> list[str]:
    """Message boxes: a #32770 dialog window, or a child Window inside the foreground window."""
    out = []
    try:
        for child in window_ctrl.GetChildren():
            if _role(child) == "Window" or _get(child, "ClassName", "") == "#32770":
                title = _get(child, "Name", "") or ""
                lines = [(_get(c, "Name", "") or "") for c, _d in _walk(child, 4)
                         if _role(c) == "Text"]
                txt = " — ".join(x for x in [title, *lines] if x)
                if txt:
                    out.append(policy_bridge.clean(txt, 300))
    except Exception:
        pass
    if _get(window_ctrl, "ClassName", "") == "#32770":
        lines = [(_get(c, "Name", "") or "") for c, _d in kept if _role(c) == "Text"]
        out.insert(0, policy_bridge.clean(" — ".join([_get(window_ctrl, "Name", "") or "", *lines]), 300))
    return out[:5]


def context(window_ctrl=None, *, max_elements: int = 250, text_limit: int = 3000,
            time_budget: float = 1.5, include_windows: bool = True) -> ScreenContext:
    """Screen context for one window (default: the foreground one)."""
    t0 = time.monotonic()
    with _com():
        ctrl = window_ctrl or _foreground()
        if ctrl is None:
            return ScreenContext(window=None, notes=["no foreground window"])
        win = to_window(ctrl, foreground=window_ctrl is None)
        ctx = ScreenContext(window=win)
        if include_windows:
            ctx.windows = [w for w in list_windows() if w.hwnd != win.hwnd][:10]
        if win.sensitive:
            ctx.notes.append(f"contents hidden: {win.sensitive}")
            ctx.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return ctx
        kept, ctx.truncated = walk(ctrl, max_elements=max_elements, time_budget=time_budget)
        ctx.elements = [to_element(c, d) for c, d in kept]
        texts, budget = [], text_limit
        for c, _d in kept:
            if budget <= 0:
                break
            role = _role(c)
            if role == "Document" and not _get(c, "IsPassword", False):
                t = _doc_text(c, budget)
            elif role in _TEXTUAL:
                t = _get(c, "Name", "") or ""
            else:
                continue
            t = policy_bridge.clean_block(t, budget)
            if t and t not in texts:
                texts.append(t)
                budget -= len(t)
        ctx.text = "\n".join(texts)[:text_limit]
        try:
            f = _focused()
            if f is not None and int(_get(f, "ProcessId", 0) or 0) == win.pid:
                ctx.focused = to_element(f)
                if not _get(f, "IsPassword", False):
                    ctx.selection = policy_bridge.clean(_selection_text(f, 500), 500)
        except Exception:
            pass
        if not ctx.selection:
            sel = [e.name for e in ctx.elements if e.selected and e.name]
            ctx.selection = policy_bridge.clean(", ".join(sel[:10]), 300)
        ctx.dialogs = _dialogs(ctrl, kept)
        ctx.elapsed_ms = int((time.monotonic() - t0) * 1000)
        return ctx
