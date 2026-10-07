"""Desktop intelligence: what's on screen, and where a control is (read-only).

Reading the screen happens here, in-process, through Windows UI Automation (fast, text, no
screenshots). ACTING stays with cua-driver (the `mcp__computer__*` tools), which already clicks
and types by UIA element, behind Helios's gate, panic stop and single-driver lock. This package
hands the brain the exact pid / window_id / control name to act on, so it can go straight to
`get_window_state(query=...)` + `click(element_index)` instead of reading screenshots.
See docs/HELIOS_WINDOWS_CONTROL.md.
"""
