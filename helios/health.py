"""Project health (blueprint phase 9): one PROJECT HEALTH report per configured project.

For every project in the manifests:
  - Build / Tests / Lint / Typecheck — the manifest's own health checks (projects.run_checks), each
    grouped by kind; a kind with no check says "not configured" (never implied to pass);
  - Git — branch, uncommitted files and how long the oldest has waited, commits not pushed / behind
    as of the last fetch (never fetches), diff size, last commit;
  - Dependencies — `dependencies: auto|npm|pip|off` in the manifest. npm: `npm outdated` +
    `npm audit` (+ node_modules present); pip: the project's own venv `pip check` +
    `pip list --outdated`. Fixed, read-only commands run by Helios, no shell;
  - Built-in probes the manifest opts into (`builtin_checks: [helios_runtime, helios_voice,
    helios_memory]`) — Helios's own runtime / voice / memory health;
  - Potential issues, and one verdict: FAILING / ATTENTION / OK / UNKNOWN.

Nothing here pushes, publishes, deploys, installs, upgrades or deletes anything. Results are stored
in each project's state file; reading a report never re-runs anything except cheap local git.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from . import conf, projects

ROWS = (("build", "Build"), ("test", "Tests"), ("lint", "Lint"), ("typecheck", "Typecheck"),
        ("other", "Other checks"))
UNCOMMITTED_DAYS = 3        # uncommitted work older than this becomes a potential issue
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_BELOW_NORMAL = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)   # background: never compete with voice


# ------------------------------------------------------------------------------ helpers

def _run(argv: list[str], cwd: str, timeout: int = 180) -> tuple[int | None, str]:
    """A fixed, read-only command (no shell). (exit code or None on error/timeout, stdout)."""
    env = dict(os.environ, CI="1", NO_COLOR="1", FORCE_COLOR="0", npm_config_update_notifier="false",
               npm_config_fund="false", PIP_DISABLE_PIP_VERSION_CHECK="1", PYTHONIOENCODING="utf-8")
    try:
        r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env, stdin=subprocess.DEVNULL,
                           creationflags=_NO_WINDOW | _BELOW_NORMAL)
        return r.returncode, r.stdout or ""
    except Exception as e:
        conf.log("health", f"{argv[0]} failed: {e}")
        return None, ""


def _major(v: str | None) -> str:
    m = re.match(r"\D*(\d+)", str(v or ""))
    return m.group(1) if m else ""


def _json(text: str):
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"[\[{].*[\]}]", text or "", re.S)
        try:
            return json.loads(m.group(0)) if m else None
        except Exception:
            return None


def _ago(iso: str | None) -> str:
    try:
        d = datetime.now() - datetime.fromisoformat(str(iso)[:19])
    except Exception:
        return "?"
    if d < timedelta(hours=1):
        return f"{int(d.total_seconds() // 60)} min ago"
    if d < timedelta(days=1):
        return f"{int(d.total_seconds() // 3600)} h ago"
    return f"{d.days} d ago"


# ------------------------------------------------------------------------------ dependencies

def _npm(root: Path) -> dict:
    res = {"manager": "npm", "outdated": [], "major": 0, "vulnerabilities": {}, "problems": []}
    npm = shutil.which("npm")
    if not npm:
        return res | {"status": "skip", "summary": "npm not installed"}
    if not (root / "node_modules").is_dir():
        res["problems"].append("dependencies aren't installed (no node_modules)")
    code, out = _run([npm, "outdated", "--json"], str(root))     # exit 1 = something is outdated
    data = _json(out) if code is not None else None
    if isinstance(data, dict):
        for name, info in data.items():
            if not isinstance(info, dict):
                continue
            cur, latest = info.get("current"), info.get("latest")
            res["outdated"].append({"name": name, "current": cur, "latest": latest})
            if cur and latest and _major(cur) != _major(latest):
                res["major"] += 1
    elif code is None:
        res["error"] = "npm outdated did not finish"
    code, out = _run([npm, "audit", "--json"], str(root))
    data = _json(out) if code is not None else None
    if isinstance(data, dict):
        v = (data.get("metadata") or {}).get("vulnerabilities") or {}
        res["vulnerabilities"] = {k: int(v.get(k) or 0) for k in ("critical", "high", "moderate", "low")
                                  if int(v.get(k) or 0)}
        per_pkg = data.get("vulnerabilities") or {}
        res["vulnerable"] = sorted(
            ({"name": n, "severity": str(i.get("severity") or "")} for n, i in per_pkg.items()
             if isinstance(i, dict)),
            key=lambda x: ("critical", "high", "moderate", "low").index(x["severity"])
            if x["severity"] in ("critical", "high", "moderate", "low") else 9)[:10]
    return res


def _venv_python(root: Path) -> Path | None:
    for rel in (".venv/Scripts/python.exe", "venv/Scripts/python.exe", ".venv/bin/python", "venv/bin/python"):
        if (root / rel).exists():
            return root / rel
    return None


def _pip(root: Path) -> dict:
    res = {"manager": "pip", "outdated": [], "major": 0, "vulnerabilities": {}, "problems": []}
    py = _venv_python(root)
    if not py:
        return res | {"status": "skip", "summary": "no project virtualenv found (.venv / venv)"}
    code, out = _run([str(py), "-m", "pip", "check"], str(root), 120)
    if code not in (0, None):
        # Declared-but-missing requirements are worth knowing, but often harmless (e.g. an old
        # package still listing the `typing` backport), so they warn rather than fail.
        res["warnings"] = [l.strip() for l in out.splitlines() if l.strip()][:5] or ["pip check failed"]
    code, out = _run([str(py), "-m", "pip", "list", "--outdated", "--format=json"], str(root), 300)
    data = _json(out) if code == 0 else None
    if isinstance(data, list):
        for d in data:
            cur, latest = d.get("version"), d.get("latest_version")
            res["outdated"].append({"name": d.get("name"), "current": cur, "latest": latest})
            if _major(cur) != _major(latest):
                res["major"] += 1
    else:
        res["error"] = "pip list --outdated did not finish (offline?)"
    return res


def dependency_status(p: dict) -> dict:
    """Run the dependency check the manifest asks for. Always returns a dict with 'status'."""
    mode = p.get("dependencies", "auto")
    root = Path(p["path"])
    if mode == "off":
        res = {"status": "off", "summary": "turned off in the manifest"}
    elif not p["usable"]:
        res = {"status": "skip", "summary": "project folder unusable"}
    elif mode == "npm" or (mode == "auto" and (root / "package.json").exists()):
        res = _npm(root)
    elif mode == "pip" or (mode == "auto" and ((root / "pyproject.toml").exists()
                                               or (root / "requirements.txt").exists())):
        res = _pip(root)
    else:
        res = {"status": "skip", "summary": "no supported package manager (npm / pip) detected"}
    if "status" not in res:
        vulns = res.get("vulnerabilities", {})
        # fail = broken (not installed / can't resolve); advisories and old versions = attention.
        if res["problems"]:
            res["status"] = "fail"
        elif vulns or res.get("major") or res.get("error") or res.get("warnings"):
            res["status"] = "warn"
        else:
            res["status"] = "ok"
        bits = [f"{len(res['outdated'])} outdated" + (f" ({res['major']} major)" if res["major"] else "")]
        bits.append("vulnerabilities: " + ", ".join(f"{n} {k}" for k, n in vulns.items())
                    if vulns else "no known vulnerabilities" if res["manager"] == "npm" else "")
        bits += res["problems"][:2]
        if res.get("warnings"):
            bits.append(f"pip check: {len(res['warnings'])} declared requirement(s) not met")
        if res.get("error"):
            bits.append(res["error"])
        res["summary"] = f"{res['manager']}: " + " · ".join(b for b in bits if b)
    res["at"] = datetime.now().isoformat(timespec="seconds")
    return res


# ------------------------------------------------------------------------------ built-in probes

def _probe(status: str, items: list[str], issues: list[str]) -> dict:
    return {"status": status, "items": items, "issues": issues,
            "at": datetime.now().isoformat(timespec="seconds")}


def _worst(*s: str) -> str:
    order = {"fail": 3, "warn": 2, "ok": 1}
    return max(s, key=lambda x: order.get(x, 0)) if s else "ok"


def probe_helios_runtime(p: dict) -> dict:
    items, issues, st = [], [], []
    try:
        with urllib.request.urlopen(f"{conf.BASE_URL}/health", timeout=2) as r:
            running = r.status == 200
    except Exception:
        running = False
    items.append("app: running" if running else "app: not running")
    if not running:
        st.append("warn")
        issues.append("Helios isn't running — Night Mode and reminders need it")
    engine = conf.brain_engine()
    if engine == "antigravity":
        from . import agy_cli
        ok = bool(agy_cli.command())
        items.append(f"brain: Antigravity CLI {'found' if ok else 'NOT FOUND'}")
        if not ok:
            st.append("fail")
            issues.append("the Antigravity CLI (agy) is missing — Helios can't think")
    else:
        items.append(f"brain: {engine}")
    try:
        from .night_mode import scheduler as night
        hb = night._state().get("heartbeat")
        if running and hb and datetime.now() - datetime.fromisoformat(hb) > timedelta(minutes=5):
            st.append("warn")
            issues.append(f"the background scheduler last ticked {_ago(hb)}")
        last = next(iter(night.runs(1)), None)
        if last:
            items.append(f"last night run: {last.get('night')} {last.get('status')}")
            if last.get("status") in ("crashed", "missed", "interrupted"):
                st.append("warn")
                issues.append(f"last Night Mode run {last.get('status')}"
                              + (f": {last.get('reason')}" if last.get("reason") else ""))
    except Exception as e:
        items.append(f"night mode: unknown ({e})")
    floor = float(conf.proactive_cfg().get("disk_gb", 10))
    for drive in ("C:\\", "D:\\"):
        try:
            free = shutil.disk_usage(drive).free / 1e9
        except Exception:
            continue
        items.append(f"free on {drive[:2]}: {free:.0f} GB")
        if free < floor:
            st.append("warn")
            issues.append(f"low disk space on {drive[:2]} ({free:.0f} GB free)")
    return _probe(_worst(*st), items, issues)


def _voice_daemon_running() -> bool:
    try:
        d = json.loads(conf.VOICE_PID_FILE.read_text(encoding="utf-8"))
        import psutil
        proc = psutil.Process(int(d["pid"]))
        return any("daemon.py" in part for part in proc.cmdline())
    except Exception:
        return False


def probe_helios_voice(p: dict) -> dict:
    cfg = conf.voice_cfg()
    items, issues, st = [], [], []
    enabled = bool(cfg.get("enabled"))
    running = _voice_daemon_running()
    items.append(f"voice: {'enabled' if enabled else 'disabled'}, listener {'running' if running else 'not running'}")
    if enabled and not running:
        st.append("warn")
        issues.append("voice is enabled but the listener isn't running (restart Helios)")
    if str(cfg.get("tts_engine", "kokoro")).lower() == "kokoro":
        missing = [f.name for f in (conf.KOKORO_MODEL, conf.KOKORO_VOICES) if not f.exists()]
        items.append("text-to-speech: " + (f"MISSING {', '.join(missing)}" if missing else "model files present"))
        if missing:
            st.append("fail")
            issues.append("the Kokoro voice files are missing — Helios can't speak")
    try:
        from .voice.wake import WakeWord
        w = WakeWord(cfg.get("wake_word", "hey_jarvis"), float(cfg.get("wake_threshold", 0.5)))
        items.append("wake word: " + ("ready" if w.available else "none — clap / Ctrl+Alt+H wake instead"))
    except Exception as e:
        items.append(f"wake word: unknown ({e})")
    try:
        from .voice.diagnostics import check_voice_lock
        s, detail = check_voice_lock(cfg)
        items.append(f"voice lock: {detail}")
        if s == "fail":
            st.append("fail")
            issues.append(f"voice lock: {detail}")
    except Exception as e:
        items.append(f"voice lock: unknown ({e})")
    errors = _recent_log_errors(conf.LOGS_DIR / "voice.log", hours=24)
    if errors:
        st.append("warn")
        items.append(f"voice log: {len(errors)} error line(s) in the last 24 h")
        issues.append(f"voice errors in the last 24 h, latest: {errors[-1][:120]}")
    return _probe(_worst(*st), items, issues)


_ERR = re.compile(r"\b(error|failed|exception|traceback)\b", re.I)


def _recent_log_errors(path: Path, hours: float) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-2000:]
    except Exception:
        return []
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for l in lines:
        if l[:19] >= cutoff and _ERR.search(l) and "pytest-of-" not in l:
            out.append(l[21:].strip())
    return out


def probe_helios_memory(p: dict) -> dict:
    from . import memory_store
    items, issues, st = [], [], []
    vault = memory_store.vault()
    if not vault.is_dir():
        return _probe("fail", [f"vault: MISSING ({vault})"], [f"the memory vault is missing: {vault}"])
    try:
        probe = vault / ".helios-health-check"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        items.append(f"vault: writable ({vault})")
    except Exception as e:
        st.append("fail")
        items.append(f"vault: NOT writable ({e})")
        issues.append("the memory vault can't be written — nothing new can be remembered")
    all_items = memory_store.items()
    counts: dict[str, int] = {}
    for it in all_items:
        if memory_store.is_active(it):
            counts[it["category"]] = counts.get(it["category"], 0) + 1
    items.append("remembered: " + (", ".join(f"{n} {c}" for c, n in sorted(counts.items())) or "nothing yet"))
    pending = len(memory_store.pending())
    if pending:
        items.append(f"lessons waiting for approval: {pending}")
        issues.append(f"{pending} learned lesson(s) waiting for your approval (`helios learn review`)")
        st.append("warn")
    leaked = [it["id"] for it in all_items if memory_store.looks_secret(it["text"])]
    if leaked:
        st.append("fail")
        issues.append(f"{len(leaked)} memory item(s) look like secrets — review and forget them: {', '.join(leaked[:3])}")
    dailies = sorted((vault / "Daily").glob("*.md")) if (vault / "Daily").is_dir() else []
    if dailies:
        items.append(f"last conversation log: {dailies[-1].stem}")
    try:
        from . import research
        n = len(research.findings(limit=100000))
        items.append(f"research findings: {n}")
    except Exception:
        pass
    return _probe(_worst(*st), items, issues)


PROBES = {
    "helios_runtime": ("Runtime", probe_helios_runtime),
    "helios_voice": ("Voice", probe_helios_voice),
    "helios_memory": ("Memory", probe_helios_memory),
}


# ------------------------------------------------------------------------------ the report

def _check_row(p: dict, kind: str, results: dict) -> tuple[str, str]:
    """(status, text) for one row. status: ok | fail | skip | none (not configured) | pending."""
    checks = [c for c in p["health_checks"] if c.get("kind", "other") == kind]
    if not checks:
        return "none", "not configured"
    parts, states = [], []
    for c in checks:
        r = results.get(c["name"])
        label = c["name"] if len(checks) > 1 or c["name"] != kind else ""
        pre = f"{label}: " if label else ""
        if not r:
            parts.append(f"{pre}not run yet")
            states.append("pending")
        elif r.get("skipped"):
            parts.append(f"{pre}NOT RUN — {r['tail'][:80]}")
            states.append("skip")
        elif r["ok"]:
            parts.append(f"{pre}PASS ({r['seconds']}s, {_ago(r['at'])})")
            states.append("ok")
        else:
            last = [l for l in r["tail"].strip().splitlines() if l.strip()][-1:]
            parts.append(f"{pre}FAIL (exit {r['code']}, {_ago(r['at'])})" + (f" — {last[0][:120]}" if last else ""))
            states.append("fail")
    worst = "fail" if "fail" in states else "skip" if "skip" in states else \
        "pending" if "pending" in states else "ok"
    return worst, "; ".join(parts)


def _git_row(g: dict) -> str:
    if not g:
        return "not a git repository"
    bits = [g.get("branch") or "?"]
    if g.get("uncommitted"):
        old = g.get("oldest_change_days")
        bits.append(f"{g['uncommitted']} uncommitted" + (f" (oldest {old:.0f} d)" if old and old >= 1 else ""))
    else:
        bits.append("clean")
    if g.get("ahead"):
        bits.append(f"{g['ahead']} not pushed")
    if g.get("behind"):
        bits.append(f"{g['behind']} behind (last fetch)")
    if g.get("diffstat"):
        bits.append(g["diffstat"])
    if g.get("last_commit"):
        bits.append(f"last commit {_ago(g['last_commit'])}")
    return " · ".join(bits)


def project_report(p: dict, *, git: dict | None = None) -> dict:
    """Assemble one project's health from its stored results + fresh local git state."""
    st = projects.state(p)
    results = st.get("checks") or {}
    g = git if git is not None else projects.git_state(p, detail=True)
    rows, issues, verdicts = [], list(p["problems"]), []
    for kind, title in ROWS:
        status, text = _check_row(p, kind, results)
        if kind == "other" and status == "none":
            continue
        rows.append({"row": title, "status": status, "text": text})
        verdicts.append(status)
        if status == "fail":
            issues.append(f"{title} failing")
        elif status == "skip":
            issues.append(f"{title}: a configured check was refused or couldn't run")
    if p["usable"] and not any(c.get("kind") == "test" for c in p["health_checks"]):
        issues.append("no tests configured")
    rows.append({"row": "Git status", "status": "ok" if g else "none", "text": _git_row(g)})
    if g.get("uncommitted") and (g.get("oldest_change_days") or 0) >= UNCOMMITTED_DAYS:
        issues.append(f"uncommitted work waiting {g['oldest_change_days']:.0f} days — commit or back it up")
    if g.get("ahead"):
        issues.append(f"{g['ahead']} commit(s) not pushed")
    if g.get("behind"):
        issues.append(f"{g['behind']} commit(s) behind the remote (as of the last fetch)")
    deps = st.get("deps")
    if p.get("dependencies") == "off":
        rows.append({"row": "Dependencies", "status": "none", "text": "turned off"})
    elif deps:
        rows.append({"row": "Dependencies", "status": deps["status"],
                     "text": f"{deps.get('summary', '')} ({_ago(deps.get('at'))})"})
        verdicts.append(deps["status"] if deps["status"] not in ("skip", "off") else "none")
        issues += list(deps.get("problems") or [])
        issues += [f"pip check: {w}" for w in (deps.get("warnings") or [])[:3]]
        v = deps.get("vulnerabilities") or {}
        if v:
            who = ", ".join(f"{x['name']} ({x['severity']})" for x in (deps.get("vulnerable") or [])[:4])
            issues.append("security advisories: " + ", ".join(f"{n} {k}" for k, n in v.items())
                          + (f" — {who}" if who else "") + " (`npm audit` for details; fixes are yours to apply)")
        if deps.get("major"):
            issues.append(f"{deps['major']} dependency major version(s) behind")
    else:
        rows.append({"row": "Dependencies", "status": "pending", "text": "not checked yet"})
    for name in p.get("builtin_checks", []):
        title = PROBES[name][0]
        pr = (st.get("probes") or {}).get(name)
        if not pr:
            rows.append({"row": title, "status": "pending", "text": "not checked yet"})
            continue
        rows.append({"row": title, "status": pr["status"],
                     "text": "; ".join(pr["items"]) + f" ({_ago(pr.get('at'))})"})
        verdicts.append(pr["status"])
        issues += pr["issues"]
    if p["health_checks"]:
        last = st.get("last_run")
        if not last:
            issues.append("health checks never run")
        elif datetime.now() - datetime.fromisoformat(last) > timedelta(days=p["stale_days"]):
            issues.append(f"health checks last run {_ago(last)}")
    checked = any(v not in ("none", "pending") for v in verdicts)
    if "fail" in verdicts or not p["usable"]:
        verdict = "FAILING"
    elif not checked:
        verdict = "UNKNOWN"          # nothing has actually been checked yet (issues still listed)
    elif issues or "warn" in verdicts:
        verdict = "ATTENTION"
    else:
        verdict = "OK"
    return {"project": p["name"], "verdict": verdict, "rows": rows,
            "issues": list(dict.fromkeys(issues)), "at": datetime.now().isoformat(timespec="seconds")}


def reports(name: str | None = None) -> list[dict]:
    return [project_report(p) for p in projects._select(name)]


def run_all(name: str | None = None, *, deps: bool = True, probes: bool = True) -> list[dict]:
    """Run everything for one project (or all active ones): the manifest's checks, the dependency
    check and the opted-in probes; store the results and return the fresh reports."""
    sel = projects._select(name)
    if any(p["health_checks"] for p in sel):
        projects.run_checks(name)
    out = []
    for p in sel:
        st = projects.state(p)
        if deps and p.get("dependencies") != "off":
            t0 = time.monotonic()
            st["deps"] = dependency_status(p)
            conf.log("health", f"{p['name']}: dependencies {st['deps']['status']} "
                               f"({time.monotonic() - t0:.0f}s) — {st['deps'].get('summary', '')}")
        if probes and p.get("builtin_checks"):
            st.setdefault("probes", {})
            for b in p["builtin_checks"]:
                try:
                    st["probes"][b] = PROBES[b][1](p)
                except Exception as e:
                    st["probes"][b] = _probe("fail", [f"probe crashed: {e}"], [f"{PROBES[b][0]} probe crashed: {e}"])
        projects._save_state(p, st)
        out.append(project_report(p))
    return out


# ------------------------------------------------------------------------------ formatting

_MARK = {"ok": "PASS", "fail": "FAIL", "warn": "WARN", "skip": "NOT RUN", "none": "—", "pending": "…"}


def format_report(reps: list[dict], *, markdown: bool = False) -> str:
    if not reps:
        return "No active projects."
    lines = []
    for r in reps:
        lines.append(f"### {r['project']} — {r['verdict']}" if markdown else f"{r['project']} — {r['verdict']}")
        for row in r["rows"]:
            lines.append(f"- {row['row']}: {row['text']}" if markdown else f"  {row['row']}: {row['text']}")
        if r["issues"]:
            lines.append("- Potential issues:" if markdown else "  Potential issues:")
            lines += [f"    - {i}" if markdown else f"    - {i}" for i in r["issues"]]
        else:
            lines.append("- Potential issues: none" if markdown else "  Potential issues: none")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
