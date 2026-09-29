"""Merge a user's existing settings.toml into a freshly-shipped default — used by install.ps1
on a re-run so an update never clobbers an onboarded config.

The installer re-runs `irm install.ps1 | iex`: robocopy lands the new committed
config/settings.toml (so new keys, comments, and changed defaults arrive), then this script
overlays the user's previous values on top — their choices win, brand-new keys keep the
shipped default. secrets.toml is separate (gitignored, never copied) and untouched.

    python tools/merge_settings.py --base <new_default> --user <user_backup> --out <dest>

`--base` is the new shipped template (its comments/structure are kept), `--user` supplies the
values to preserve, `--out` is written atomically. Exits non-zero WITHOUT writing on any error
so the caller can restore the user's file verbatim (we never half-write or corrupt settings).
"""

from __future__ import annotations

import argparse
import os
import sys
import tomllib

import tomlkit


def _overlay(base: dict, user: dict) -> None:
    """Recursively set user's values into the base tomlkit doc.

    Both tomlkit tables and the stdlib dict from tomllib pass `isinstance(_, dict)`, so a
    section present in both is merged key-by-key (base keeps any keys the user lacks); a
    scalar/array, or a key the base doesn't have, is assigned wholesale (user is authoritative,
    e.g. the autonomy allow-list)."""
    for key, val in user.items():
        existing = base.get(key)
        if isinstance(val, dict) and isinstance(existing, dict):
            _overlay(existing, val)
        else:
            base[key] = val


def main() -> int:
    ap = argparse.ArgumentParser(description="Merge user settings.toml values onto a new default.")
    ap.add_argument("--base", required=True, help="new shipped default settings.toml (template)")
    ap.add_argument("--user", required=True, help="backup of the user's existing settings.toml")
    ap.add_argument("--out", required=True, help="where to write the merged result (atomic)")
    args = ap.parse_args()

    try:
        with open(args.base, "rb") as fh:
            base_doc = tomlkit.parse(fh.read().decode("utf-8"))
    except Exception as e:  # no usable new default -> let the caller keep the user's file
        print(f"merge_settings: cannot read base ({e})", file=sys.stderr)
        return 1
    try:
        with open(args.user, "rb") as fh:
            user_data = tomllib.load(fh)
    except Exception as e:  # unreadable/corrupt user backup -> don't risk a bad merge
        print(f"merge_settings: cannot read user settings ({e})", file=sys.stderr)
        return 1

    try:
        _overlay(base_doc, user_data)
        rendered = tomlkit.dumps(base_doc)
        tmp = args.out + ".tmp"
        # newline="" disables Windows text-mode \n->\r\n translation: tomlkit already preserves
        # the source's line endings (settings.toml is CRLF), so translating again would yield
        # \r\r\n and a lone \r that breaks tomllib. Write tomlkit's bytes verbatim.
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(rendered)
        os.replace(tmp, args.out)  # atomic — a crash mid-write can't corrupt settings.toml
    except Exception as e:
        print(f"merge_settings: merge failed ({e})", file=sys.stderr)
        return 1

    print("merge_settings: preserved your existing settings.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
