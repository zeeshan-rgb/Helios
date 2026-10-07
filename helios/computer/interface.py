"""Backend-neutral types for screen reading (UIA today; OCR / vision fill the same shapes)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Window:
    hwnd: int                 # == cua-driver window_id
    pid: int
    app: str                  # process name, e.g. "notepad.exe"
    title: str
    class_name: str = ""
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)   # x, y, w, h (screen pixels)
    foreground: bool = False
    sensitive: str = ""       # why its contents are hidden ("" = readable)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Element:
    role: str                 # "button", "edit", "menu item", ...
    name: str
    value: str = ""
    automation_id: str = ""
    enabled: bool = True
    focused: bool = False
    selected: bool = False
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    depth: int = 0
    source: str = "uia"       # uia | ocr

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScreenContext:
    window: Window | None
    windows: list[Window] = field(default_factory=list)       # other visible top-level windows
    focused: Element | None = None
    elements: list[Element] = field(default_factory=list)     # filtered, actionable / named
    text: str = ""            # visible document / static text (bounded)
    selection: str = ""
    dialogs: list[str] = field(default_factory=list)          # message boxes / errors
    ocr_lines: list[Element] = field(default_factory=list)    # text read from pixels (fallback)
    ocr_text: str = ""
    truncated: bool = False
    notes: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
