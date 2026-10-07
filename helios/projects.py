"""Project intelligence: user-authored project manifests + read-only project state.

One YAML manifest per project in conf.projects_dir() (default data/projects):

    name: Maqsusi
    path: C:/Users/me/Desktop/Claude/3d Website/maqsusi
    active: true
    description: Immersive 3D media site
    technology: [nextjs, react, three.js]
    status: MVP built                      # free-text status metadata
    commands: {test: "", build: npm run build, lint: npm run lint}
    health_checks:                         # what "run the health checks" runs
      - {name: lint, run: npm run lint, timeout: 300}
    research_topics: [react three fiber performance]
    stale_days: 3                          # flag when checks are older than this

The manifests ARE the allowlist: Helios inspects and runs checks only in folders listed here, never
in sensitive locations (SSH keys, cloud/API credentials, browser or password-manager data, system
folders, a whole drive or the home folder). Checks run only the commands the user wrote, without a
shell, with a timeout, and never anything that pushes, publishes, deploys or deletes. Git state is
read with fixed read-only git commands. Results go to <projects_dir>/.state/<slug>.json, so the
user's YAML is never rewritten. The brain's permission gate hard-denies edits to this folder.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import conf

DEFAULT_TIMEOUT = 600
MAX_TIMEOUT = 3600
DEFAULT_STALE_DAYS = 3
_TAIL = 1500
_SCAN_SKIP = {".git", "node_modules", ".next", ".venv", "venv", "__pycache__", "build", "dist",
              ".gradle", ".kotlin", "target", ".idea", ".pytest_cache", "out", "coverage", ".turbo",
              ".cache", ".mypy_cache", ".ruff_cache"}
_SCAN_MAX_ENTRIES = 20000

# ------------------------------------------------------------------------------ safety

_METACHARS = re.compile(r"[&|;<>^%`$]|\r|\n")
_FORBIDDEN = [
    (re.compile(r"\bgit\s+(push|reset|clean|checkout|restore|rebase|filter-branch|gc|prune|"
                r"branch\s+-[dD]|tag\s+-d|stash\s+(drop|clear)|commit|merge|pull|am|apply)\b", re.I),
     "changes git history or the working tree"),
    (re.compile(r"\b(npm|pnpm|yarn|bun)\s+(publish|unpublish|deprecate|login|adduser|token)\b", re.I),
     "publishes a package"),
    (re.compile(r"\b(twine\s+upload|gh\s+(release|pr\s+merge|repo\s+delete)|docker\s+push|"
                r"vercel|netlify|firebase\s+deploy|fly\s+deploy|heroku|kubectl|terraform\s+apply|"
                r"deploy)\b", re.I), "deploys or publishes"),
    (re.compile(r"\b(rm|rmdir|del|erase|rd|remove-item|format|diskpart|shutdown|restart-computer|"
                r"stop-computer|reg|regedit|takeown|icacls|cipher|sdelete|mkfs|dd)\b", re.I),
     "deletes data or changes the system"),
    (re.compile(r"\b(curl|wget|invoke-webrequest|iwr|invoke-expression|iex|powershell|pwsh|cmd|"
                r"bash|sh|start-process|runas|sudo)\b", re.I), "starts a shell or downloads code"),
]


def command_problem(cmd: str) -> str | None:
    """Why a manifest command must not run (None = OK). Checks run without a shell; this is the
    defence in depth for .cmd shims (which Windows hands to cmd.exe) and for mistakes."""
    cmd = (cmd or "").strip()
    if not cmd:
        return "empty command"
    if _METACHARS.search(cmd):
        return "contains shell operators (& | ; < > ^ % ` $) — use one plain command"
    for rx, why in _FORBIDDEN:
        if rx.search(cmd):
            return f"refused: it {why}"
    return None


def _norm(p) -> str:
    return str(p).replace("\\", "/").rstrip("/").lower()


def _sensitive_roots() -> list[str]:
    home = Path.home()
    rel = [".ssh", ".aws", ".azure", ".gnupg", ".kube", ".docker", ".config/gcloud", ".claude",
           ".gemini", ".password-store", ".gitconfig", ".git-credentials", ".netrc",
           "AppData/Roaming/Microsoft/Credentials", "AppData/Local/Microsoft/Credentials",
           "AppData/Roaming/Microsoft/Protect", "AppData/Roaming/Microsoft/Crypto",
           "AppData/Local/Google/Chrome/User Data", "AppData/Local/Microsoft/Edge/User Data",
           "AppData/Roaming/Mozilla/Firefox", "AppData/Local/BraveSoftware",
           "AppData/Roaming/Opera Software", "AppData/Roaming/KeePass", "AppData/Roaming/KeePassXC",
           "AppData/Roaming/Bitwarden", "AppData/Local/1Password", "AppData/Roaming/1Password",
           "AppData/Roaming/gcloud", "AppData/Roaming/GitHub CLI"]
    roots = [_norm(home / r) for r in rel]
    windir = os.environ.get("SystemRoot") or "C:/Windows"
    roots += [_norm(windir), _norm(os.environ.get("ProgramFiles") or "C:/Program Files"),
              _norm(os.environ.get("ProgramFiles(x86)") or "C:/Program Files (x86)"),
              _norm(os.environ.get("ProgramData") or "C:/ProgramData")]
    return roots


def path_problem(path: str) -> str | None:
    """Why a folder must not be a project (None = OK)."""
    if not str(path or "").strip():
        return "no path"
    try:
        p = Path(os.path.expandvars(os.path.expanduser(str(path)))).resolve()
    except Exception:
        return "unreadable path"
    n = _norm(p)
    home = Path.home()
    if p.parent == p or re.fullmatch(r"[a-z]:", n):
        return "a whole drive is too broad — pick the project folder"
    broad = {_norm(home)} | {_norm(home / d) for d in ("Desktop", "Documents", "Downloads",
                                                      "OneDrive", "AppData")}
    if n in broad:
        return "too broad — pick the project folder itself"
    for r in _sensitive_roots():
        if n == r or n.startswith(r + "/") or r.startswith(n + "/"):
            return "a sensitive location (credentials, keys, browser or system data)"
    from . import protected                       # the one shared list of credential stores
    return protected.root_problem(str(p))


# ------------------------------------------------------------------------------ manifests

def _slug(name: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (name or "").lower())).strip("-")[:40] or "project"


def _as_list(v) -> list[str]:
    if v in (None, ""):
        return []
    if isinstance(v, str):
        return [s.strip() for s in v.split(",") if s.strip()]
    return [str(s).strip() for s in v if str(s).strip()] if isinstance(v, list) else [str(v)]


DEPENDENCY_MODES = ("auto", "npm", "pip", "off")
KINDS = ("build", "test", "lint", "typecheck")


def _kind(v) -> str:
    """Which report row a check belongs to: build / test / lint / typecheck / other."""
    s = str(v or "").lower()
    for k, words in (("typecheck", ("typecheck", "type-check", "tsc", "mypy", "pyright")),
                     ("test", ("test", "pytest", "jest", "vitest")),
                     ("lint", ("lint", "eslint", "ruff", "flake8")),
                     ("build", ("build", "assemble", "compile"))):
        if s == k or any(w in s for w in words):
            return k
    return "other"


def _timeout(v) -> int:
    try:
        return max(5, min(MAX_TIMEOUT, int(v)))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT


def _normalize(raw: dict, file: Path) -> dict:
    """Validate one manifest into a project dict. Problems are collected, never raised."""
    problems: list[str] = []
    name = str(raw.get("name") or file.stem).strip()[:60]
    path = str(raw.get("path") or "").strip()
    pp = path_problem(path)
    if pp:
        problems.append(f"path: {pp}")
    elif not Path(path).is_dir():
        problems.append("path: folder not found")
    commands = raw.get("commands") if isinstance(raw.get("commands"), dict) else {}
    commands = {k: str(v).strip() for k, v in commands.items() if v not in (None, "")}
    checks = []
    for i, c in enumerate(raw.get("health_checks") or []):
        if isinstance(c, str):
            c = {"run": c}
        if not isinstance(c, dict):
            continue
        run = str(c.get("run") or "").strip()
        ref_key = run if run in commands else None
        ref = commands.get(run) if ref_key else None                 # "run: test" -> commands.test
        run = ref or run
        cname = str(c.get("name") or (ref and c.get("run")) or run.split(" ")[0] or f"check{i + 1}")[:40]
        prob = command_problem(run)
        checks.append({"name": cname, "run": run, "timeout": _timeout(c.get("timeout")),
                       "problem": prob, "kind": _kind(c.get("kind") or ref_key or cname)})
        if prob:
            problems.append(f"check {cname}: {prob}")
    try:
        stale = max(1, int(raw.get("stale_days") or DEFAULT_STALE_DAYS))
    except (TypeError, ValueError):
        stale = DEFAULT_STALE_DAYS
    dv = raw.get("dependencies", "auto")
    # YAML 1.1 reads a bare off/no as False and on/yes as True.
    deps = "off" if dv is False else "auto" if dv in (True, None, "") else str(dv).strip().lower()
    if deps not in DEPENDENCY_MODES:
        problems.append(f"dependencies: unknown value {deps!r} (use {', '.join(DEPENDENCY_MODES)})")
        deps = "off"
    builtin = _as_list(raw.get("builtin_checks"))
    from . import health
    for b in [b for b in builtin if b not in health.PROBES]:
        problems.append(f"builtin_checks: unknown check {b!r} (known: {', '.join(health.PROBES)})")
    builtin = [b for b in builtin if b in health.PROBES]
    return {
        "name": name, "slug": _slug(name), "path": path, "file": str(file),
        "active": raw.get("active", True) is not False,
        "description": str(raw.get("description") or "").strip(),
        "technology": _as_list(raw.get("technology")),
        "status": str(raw.get("status") or "").strip(),
        "commands": commands, "health_checks": checks,
        "research_topics": _as_list(raw.get("research_topics")),
        "stale_days": stale, "problems": problems,
        "dependencies": deps, "builtin_checks": builtin,
        "usable": not (pp or not Path(path).is_dir()) if path else False,
    }


def load_all() -> tuple[list[dict], list[str]]:
    """(projects, errors). A manifest that doesn't parse is reported, not fatal."""
    import yaml
    d = conf.projects_dir()
    projects, errors = [], []
    if not d.is_dir():
        return projects, errors
    for f in sorted(list(d.glob("*.yaml")) + list(d.glob("*.yml"))):
        try:
            raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                raise ValueError("not a mapping")
        except Exception as e:
            errors.append(f"{f.name}: cannot read ({str(e).splitlines()[0][:120]})")
            continue
        projects.append(_normalize(raw, f))
    return projects, errors


def get(name: str) -> dict | None:
    """Find a project by name / slug / unique prefix (case-insensitive)."""
    q = (name or "").strip().lower()
    if not q:
        return None
    ps, _ = load_all()
    for p in ps:
        if q in (p["name"].lower(), p["slug"]):
            return p
    hits = [p for p in ps if p["name"].lower().startswith(q) or p["slug"].startswith(_slug(q))]
    return hits[0] if len(hits) == 1 else None


def active() -> list[dict]:
    return [p for p in load_all()[0] if p["active"]]


def _select(name: str | None) -> list[dict]:
    if name:
        p = get(name)
        if not p:
            raise KeyError(f"no project called {name!r} (see list_projects)")
        return [p]
    return active()


# ------------------------------------------------------------------------------ state

def _state_file(p: dict) -> Path:
    return conf.projects_dir() / ".state" / f"{p['slug']}.json"


def state(p: dict) -> dict:
    try:
        return json.loads(_state_file(p).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(p: dict, st: dict) -> None:
    f = _state_file(p)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(st, indent=1), encoding="utf-8")


# ------------------------------------------------------------------------------ git (read-only)

def _git(path: str, *args: str, timeout: int = 30) -> tuple[int, str]:
    """Fixed read-only git queries. Repo config can't run code here: fsmonitor/pager are off."""
    git = shutil.which("git")
    if not git:
        return 127, "git not installed"
    env = dict(os.environ, GIT_PAGER="cat", GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    try:
        r = subprocess.run([git, "-c", "core.fsmonitor=false", "-c", "core.pager=cat",
                            "--no-pager", "-C", path, *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, env=env,
                           stdin=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        # rstrip only: porcelain lines start with a meaningful space (" M file").
        return r.returncode, (r.stdout or r.stderr or "").rstrip().lstrip("\r\n")
    except Exception as e:
        return 1, str(e)


def is_git(path: str) -> bool:
    return (Path(path) / ".git").exists() and _git(path, "rev-parse", "--is-inside-work-tree")[0] == 0


def git_state(p: dict, *, detail: bool = False) -> dict:
    """Branch + uncommitted files. detail=True adds (all local, never fetches): commits ahead of /
    behind the upstream as of the last fetch, the diff size, how old the oldest uncommitted change
    is, and the last commit."""
    if not p["usable"] or not is_git(p["path"]):
        return {}
    _, branch = _git(p["path"], "rev-parse", "--abbrev-ref", "HEAD")
    code, porcelain = _git(p["path"], "status", "--porcelain=v1")
    changed = [l for l in porcelain.splitlines() if l.strip()] if code == 0 else []
    out = {"branch": branch, "uncommitted": len(changed), "changed_files": changed[:20]}
    if not detail:
        return out
    code, lr = _git(p["path"], "rev-list", "--left-right", "--count", "HEAD...@{upstream}")
    if code == 0 and len(lr.split()) == 2:
        out["ahead"], out["behind"] = (int(x) for x in lr.split())
    else:
        out["ahead"] = out["behind"] = None          # no upstream branch configured
    code, stat = _git(p["path"], "diff", "--shortstat", "HEAD")
    out["diffstat"] = stat.strip() if code == 0 else ""
    code, last = _git(p["path"], "log", "-1", "--format=%cI %s")
    if code == 0 and last:
        when, _, subject = last.partition(" ")
        out["last_commit"], out["last_commit_subject"] = when[:19], subject[:100]
    oldest = None
    for line in changed[:200]:
        rel = line[3:].split(" -> ")[-1].strip().strip('"')
        try:
            m = os.path.getmtime(os.path.join(p["path"], rel))
        except OSError:
            continue
        oldest = m if oldest is None else min(oldest, m)
    out["oldest_change_days"] = round((time.time() - oldest) / 86400, 1) if oldest else None
    return out


# ------------------------------------------------------------------------------ changes

def changes(p: dict, since: datetime) -> dict:
    """What changed in one project since `since`: commits + uncommitted files (git), or recently
    modified files (a bounded walk that skips dependency/build folders) for non-git folders."""
    out = {"project": p["name"], "since": since.isoformat(timespec="minutes"), "git": False,
           "commits": [], "uncommitted": [], "files": [], "error": ""}
    if not p["usable"]:
        out["error"] = "; ".join(p["problems"]) or "unusable"
        return out
    if is_git(p["path"]):
        out["git"] = True
        code, log = _git(p["path"], "log", f"--since={since.isoformat(timespec='seconds')}",
                         "--pretty=format:%h %ad %s", "--date=format:%Y-%m-%d %H:%M", "-n", "50")
        out["commits"] = log.splitlines() if code == 0 and log else []
        out["uncommitted"] = git_state(p).get("changed_files", [])
        return out
    cutoff = since.timestamp()
    hits, seen = [], 0
    for root, dirs, files in os.walk(p["path"]):
        dirs[:] = [d for d in dirs if d not in _SCAN_SKIP and not d.startswith(".")]
        for f in files:
            seen += 1
            if seen > _SCAN_MAX_ENTRIES:
                out["error"] = f"stopped after {_SCAN_MAX_ENTRIES} files"
                break
            fp = os.path.join(root, f)
            try:
                m = os.path.getmtime(fp)
            except OSError:
                continue
            if m >= cutoff:
                hits.append((m, os.path.relpath(fp, p["path"])))
        if seen > _SCAN_MAX_ENTRIES:
            break
    hits.sort(reverse=True)
    out["files"] = [f"{datetime.fromtimestamp(m):%Y-%m-%d %H:%M} {rel}" for m, rel in hits[:30]]
    out["files_total"] = len(hits)
    return out


def all_changes(hours: float = 24, name: str | None = None) -> list[dict]:
    since = datetime.now() - timedelta(hours=hours)
    return [changes(p, since) for p in _select(name)]


# ------------------------------------------------------------------------------ health checks

def _resolve_argv(cmd: str, cwd: str) -> list[str]:
    if os.name == "nt":                 # a backslash here is a path separator, not an escape
        cmd = cmd.replace("\\", "/")
    argv = shlex.split(cmd, posix=True)
    exe = argv[0]
    local = Path(cwd) / exe
    if ("/" in exe or "\\" in exe) and local.exists():
        argv[0] = str(local)
    else:
        found = shutil.which(exe, path=os.pathsep.join([cwd, os.environ.get("PATH", "")]))
        if not found:
            raise FileNotFoundError(f"{exe} not found")
        argv[0] = found
    return argv


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            proc.kill()
    except Exception:
        pass


def run_check(p: dict, check: dict) -> dict:
    """Run one configured check (no shell, timeout, output tail kept and redacted)."""
    from .memory import _redact
    res = {"name": check["name"], "run": check["run"], "ok": False, "code": None,
           "at": datetime.now().isoformat(timespec="seconds"), "seconds": 0.0, "tail": ""}
    if check.get("problem") or command_problem(check["run"]):
        res["tail"] = check.get("problem") or command_problem(check["run"])
        res["skipped"] = True
        return res
    if not p["usable"]:
        res["tail"] = "; ".join(p["problems"])
        res["skipped"] = True
        return res
    t0 = time.monotonic()
    try:
        argv = _resolve_argv(check["run"], p["path"])
        env = dict(os.environ, CI="1", FORCE_COLOR="0", NO_COLOR="1", PYTHONIOENCODING="utf-8")
        proc = subprocess.Popen(argv, cwd=p["path"], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))
        try:
            raw, _ = proc.communicate(timeout=check["timeout"])
            res["code"] = proc.returncode
            res["ok"] = proc.returncode == 0
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            raw = (proc.communicate(timeout=10)[0] or b"") if proc.stdout else b""
            res["tail"] = f"timed out after {check['timeout']}s\n"
        text = (raw or b"").decode("utf-8", errors="replace")
        res["tail"] += _redact(re.sub(r"\x1b\[[0-9;]*m", "", text))[-_TAIL:]
    except Exception as e:
        res["tail"] = f"could not run: {e}"
    res["seconds"] = round(time.monotonic() - t0, 1)
    return res


class Busy(RuntimeError):
    pass


def _lock_path() -> Path:
    return conf.projects_dir() / ".state" / "checks.lock"


def _acquire() -> None:
    f = _lock_path()
    f.parent.mkdir(parents=True, exist_ok=True)
    try:
        if f.exists() and time.time() - f.stat().st_mtime > MAX_TIMEOUT * 2:
            f.unlink()                                  # stale lock from a crashed run
        fd = os.open(str(f), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        raise Busy("health checks are already running")


def run_checks(name: str | None = None, only: str | None = None) -> list[dict]:
    """Run the configured health checks for one project (or every active one). Returns
    [{project, git, results:[...]}] and records the results in the project's state file."""
    _acquire()
    try:
        report = []
        for p in _select(name):
            checks = [c for c in p["health_checks"]
                      if not only or c["name"].lower() == only.strip().lower()]
            results = []
            for c in checks:
                r = run_check(p, c)
                results.append(r)
                conf.log("projects", f"{p['name']}: {c['name']} -> "
                                     f"{'skipped' if r.get('skipped') else ('ok' if r['ok'] else 'FAIL')}"
                                     f" ({r['seconds']}s)")
            st = state(p)
            st.setdefault("checks", {})
            for r in results:
                st["checks"][r["name"]] = r
            st["git"] = git_state(p)
            st["last_run"] = datetime.now().isoformat(timespec="seconds")
            _save_state(p, st)
            report.append({"project": p["name"], "git": st["git"], "results": results,
                           "problems": p["problems"]})
        return report
    finally:
        _lock_path().unlink(missing_ok=True)


# ------------------------------------------------------------------------------ status

def failing(p: dict, st: dict | None = None) -> list[dict]:
    st = state(p) if st is None else st
    names = {c["name"] for c in p["health_checks"]}
    return [r for n, r in (st.get("checks") or {}).items()
            if n in names and not r.get("ok") and not r.get("skipped")]


def attention(p: dict) -> list[str]:
    """Reasons this project needs the user's attention (empty = fine)."""
    reasons = list(p["problems"])
    if not p["usable"]:
        return reasons
    st = state(p)
    for r in failing(p, st):
        reasons.append(f"{r['name']} failing (exit {r['code']}, {r['at'][:16].replace('T', ' ')})")
    if p["health_checks"]:
        last = st.get("last_run")
        if not last:
            reasons.append("health checks never run")
        elif datetime.now() - datetime.fromisoformat(last) > timedelta(days=p["stale_days"]):
            reasons.append(f"health checks last run {last[:10]}")
    g = git_state(p)
    if g.get("uncommitted"):
        reasons.append(f"{g['uncommitted']} uncommitted change(s) on {g.get('branch', '?')}")
    return reasons


# ------------------------------------------------------------------------------ discover (CLI only)

_DISCOVER_SKIP = _SCAN_SKIP | {"appdata", "windows", "program files", "program files (x86)",
                               "programdata", "$recycle.bin", "system volume information",
                               "onedrivetemp", "temp", "tmp", "cache", "caches", "logs",
                               "site-packages", ".vscode", ".idea", "packages", "bin", "obj",
                               # tool caches / SDKs / raw database stores / licences — not "work"
                               "npm-cache", "maven-repo", ".m2", "gradle", "android", "sdk",
                               "nodejs", "postgres-data", "pgdata", "pg_data", "license",
                               "licenses", "uploaddir"}
_MARKERS = (".git", "package.json", "pyproject.toml", "requirements.txt", "build.gradle",
            "build.gradle.kts", "pom.xml", "Cargo.toml", "go.mod", ".project", "Makefile")


def default_discovery_roots() -> list[Path]:
    home = Path.home()
    roots = [home / "Desktop", home / "Documents", home / "Downloads"]
    for drive in ("D:\\", "E:\\"):
        if Path(drive).exists():
            roots.append(Path(drive))
    return [r for r in roots if r.exists()]


def discover_recent(days: int = 60, roots: list[Path] | None = None, *, max_depth: int = 3,
                    max_entries: int = 250_000, time_budget: float = 90.0) -> list[dict]:
    """Folders you've worked in lately: a candidate is a folder (<= max_depth below a root)
    holding files modified in the last `days` days — rolled up to the nearest project root
    (a folder with .git / package.json / pyproject ...). Skips system, app-data, dependency and
    build folders, protected locations and Helios's own data. Read-only: lists, never adds."""
    from . import protected
    cutoff = time.time() - days * 86400
    started = time.monotonic()
    known = {_norm(p["path"]) for p in load_all()[0]}
    helios_data = {_norm(conf.DATA_DIR), _norm("D:/Helios")}
    found: dict[str, dict] = {}
    seen = 0
    for root in (roots or default_discovery_roots()):
        base_depth = len(root.parts)
        # roll plain folders up to their top-level working folder: one level under a home folder
        # (Desktop\Chronos), two on a data drive (E:\data\workspace)
        rollup = 2 if root.parent == root else 1
        for cur, dirs, files in os.walk(root):
            if time.monotonic() - started > time_budget or seen > max_entries:
                break
            cp = Path(cur)
            depth = len(cp.parts) - base_depth
            n = _norm(cp)
            dirs[:] = [d for d in dirs
                       if d.lower() not in _DISCOVER_SKIP and not d.startswith(".")
                       and not any(_norm(cp / d).startswith(h) for h in helios_data)
                       and depth < max_depth + 2]
            prob = path_problem(str(cp)) if depth > 0 else None
            if protected.check(str(cp) + "/") or (prob and "too broad" not in prob):
                dirs[:] = []                 # credential stores / system folders: never walked
                continue
            recent = 0
            latest = 0.0
            for f in files:
                seen += 1
                try:
                    m = os.path.getmtime(os.path.join(cur, f))
                except OSError:
                    continue
                if m >= cutoff:
                    recent += 1
                    latest = max(latest, m)
            if not recent or depth == 0:
                continue
            # the project root: the nearest ancestor (or self, within max_depth) with a project
            # marker; otherwise the top-level working folder
            proj, marker = None, False
            for anc in [cp, *cp.parents]:
                d = len(anc.parts) - base_depth
                if d < 1:
                    break
                if d <= max_depth and any((anc / mk).exists() for mk in _MARKERS):
                    proj, marker = anc, True
                    break
            if proj is None:
                proj = cp
                while len(proj.parts) - base_depth > rollup:
                    proj = proj.parent
            key = _norm(proj)
            e = found.setdefault(key, {"path": str(proj), "recent_files": 0, "latest": 0.0,
                                       "git": (proj / ".git").exists(), "known": key in known,
                                       "marker": marker})
            e["recent_files"] += recent
            e["latest"] = max(e["latest"], latest)
    # a folder inside another listed folder folds into it; git repos and registered projects
    # stay separate (Docs\trunk -> Docs, App\module -> App)
    for key in sorted(found, key=len, reverse=True):
        e = found[key]
        if e["git"] or e["known"]:
            continue
        parent = next((k for k in found if k != key and key.startswith(k + "/")), None)
        if parent:
            found[parent]["recent_files"] += e["recent_files"]
            found[parent]["latest"] = max(found[parent]["latest"], e["latest"])
            del found[key]
    out = sorted(found.values(), key=lambda e: -e["latest"])
    for e in out:
        e["technology"] = detect(e["path"])["technology"]
        e["latest_iso"] = datetime.fromtimestamp(e["latest"]).isoformat(timespec="minutes")
    return out


def format_discovered(items: list[dict]) -> str:
    if not items:
        return "No recently active folders found."
    lines = []
    for i, e in enumerate(items, 1):
        tech = f" [{', '.join(e['technology'])}]" if e["technology"] else ""
        tags = (" (git)" if e["git"] else "") + (" — already a project" if e["known"] else "")
        lines.append(f"{i:>2}. {e['path']}{tech}{tags}\n      {e['recent_files']} file(s) changed, "
                     f"last {e['latest_iso'].replace('T', ' ')}")
    return "\n".join(lines)


# ------------------------------------------------------------------------------ add (CLI only)

def detect(path: str) -> dict:
    """Guess technology + commands for a folder (used by `helios projects add`)."""
    root = Path(path)
    tech, cmds = [], {}
    pkg = root / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        scripts = data.get("scripts") or {}
        deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
        tech += [t for t, k in (("nextjs", "next"), ("react", "react"), ("three.js", "three"),
                                ("typescript", "typescript"), ("vite", "vite")) if k in deps] or ["node"]
        runner = "pnpm" if (root / "pnpm-lock.yaml").exists() else (
            "yarn" if (root / "yarn.lock").exists() else "npm")
        for key in ("test", "build", "lint", "typecheck"):
            if key in scripts and "no test specified" not in str(scripts[key]):
                cmds[key] = f"{runner} test" if key == "test" and runner == "npm" else f"{runner} run {key}"
    if (root / "pyproject.toml").exists() or (root / "requirements.txt").exists():
        tech.append("python")
        py = ".venv/Scripts/python.exe" if (root / ".venv" / "Scripts" / "python.exe").exists() else "python"
        if (root / "tests").is_dir():
            cmds.setdefault("test", f"{py} -m pytest tests -q -p no:warnings -o addopts=")
    if (root / "gradlew.bat").exists() or (root / "gradlew").exists():
        tech += ["gradle", "kotlin" if any(root.glob("*.kts")) else "java"]
        gw = "gradlew.bat" if os.name == "nt" else "./gradlew"
        cmds.setdefault("test", f"{gw} test --no-daemon")
        cmds.setdefault("build", f"{gw} assembleDebug --no-daemon")
    if (root / "Cargo.toml").exists():
        tech.append("rust")
        cmds.setdefault("test", "cargo test")
        cmds.setdefault("build", "cargo build")
    # Health checks default to the light checks; builds are opt-in (they are slow and heavy).
    checks = [{"name": k, "run": k} for k in ("lint", "typecheck", "test") if k in cmds]
    return {"technology": tech, "commands": cmds, "health_checks": checks}


def add(path: str, name: str | None = None, *, overwrite: bool = False) -> Path:
    """Write a starter manifest for `path` (the user reviews/edits it). CLI only — the brain has no
    tool for this, because manifests decide which commands Helios runs."""
    import yaml
    prob = path_problem(path)
    if prob:
        raise ValueError(prob)
    folder = Path(path).resolve()
    if not folder.is_dir():
        raise ValueError("folder not found")
    name = (name or folder.name).strip()
    d = conf.projects_dir()
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{_slug(name)}.yaml"
    if f.exists() and not overwrite:
        raise FileExistsError(f"{f} already exists (edit it, or pass --force)")
    det = detect(str(folder))
    body = {"name": name, "path": folder.as_posix(), "active": True, "description": "",
            "technology": det["technology"], "status": "",
            "commands": {k: det["commands"].get(k, "") for k in ("test", "build", "lint")}
            | {k: v for k, v in det["commands"].items() if k not in ("test", "build", "lint")},
            "health_checks": det["health_checks"], "research_topics": [],
            "stale_days": DEFAULT_STALE_DAYS}
    header = ("# Helios project manifest — edit freely. Helios runs ONLY the health_checks listed\n"
              "# here (no shell; push/publish/deploy/delete commands are refused).\n")
    f.write_text(header + yaml.safe_dump(body, sort_keys=False, allow_unicode=True), encoding="utf-8")
    conf.log("projects", f"added {name} -> {f}")
    return f


# ------------------------------------------------------------------------------ formatting

def _ago(iso: str) -> str:
    try:
        d = datetime.now() - datetime.fromisoformat(iso)
    except Exception:
        return iso
    if d < timedelta(hours=1):
        return f"{int(d.total_seconds() // 60)} min ago"
    if d < timedelta(days=1):
        return f"{int(d.total_seconds() // 3600)} h ago"
    return f"{d.days} d ago"


def format_list(include_inactive: bool = False) -> str:
    ps, errors = load_all()
    ps = [p for p in ps if include_inactive or p["active"]]
    if not ps and not errors:
        return (f"No projects configured yet. Add one with `helios projects add <folder>` "
                f"(manifests live in {conf.projects_dir()}).")
    lines = []
    for p in ps:
        st = state(p)
        bad = failing(p, st)
        health = ("never checked" if not st.get("last_run") else
                  (f"{len(bad)} failing" if bad else "checks passing") + f", {_ago(st['last_run'])}")
        tech = f" [{', '.join(p['technology'])}]" if p["technology"] else ""
        status = f" — {p['status']}" if p["status"] else ""
        off = "" if p["active"] else " (inactive)"
        lines.append(f"- {p['name']}{off}{tech}{status}: {health}")
        if p["problems"]:
            lines.append(f"    problems: {'; '.join(p['problems'])}")
    lines += [f"- manifest error: {e}" for e in errors]
    return "\n".join(lines)


def format_changes(items: list[dict]) -> str:
    out = []
    for c in items:
        head = f"{c['project']} (since {c['since'].replace('T', ' ')})"
        if c["error"] and not c["files"] and not c["commits"]:
            out.append(f"{head}: {c['error']}")
            continue
        if c["git"]:
            body = [f"  commits: {len(c['commits'])}"] + [f"    {l}" for l in c["commits"][:10]]
            body += [f"  uncommitted: {len(c['uncommitted'])}"] + [f"    {l}" for l in c["uncommitted"][:10]]
            quiet = not c["commits"] and not c["uncommitted"]
        else:
            body = [f"  files modified: {c.get('files_total', 0)} (not a git repo)"]
            body += [f"    {l}" for l in c["files"][:10]]
            quiet = not c["files"]
        out.append(f"{head}: no changes" if quiet else "\n".join([head] + body))
    return "\n".join(out) or "No active projects."


def format_checks(report: list[dict]) -> str:
    out = []
    for r in report:
        out.append(r["project"])
        if not r["results"]:
            out.append("  no health checks configured")
        for x in r["results"]:
            state_ = "skipped" if x.get("skipped") else ("ok" if x["ok"] else f"FAILED (exit {x['code']})")
            out.append(f"  {x['name']}: {state_} in {x['seconds']}s")
            if not x["ok"]:
                tail = [l for l in x["tail"].strip().splitlines() if l.strip()][-6:]
                out += [f"    | {l[:200]}" for l in tail]
        if r["git"]:
            out.append(f"  git: {r['git'].get('branch')} · {r['git'].get('uncommitted', 0)} uncommitted")
    return "\n".join(out) or "No active projects."


def format_broken() -> str:
    lines = []
    for p in active():
        bad = failing(p)
        if bad or p["problems"]:
            lines.append(f"- {p['name']}: " + "; ".join(
                [f"{r['name']} failing (exit {r['code']}, {_ago(r['at'])})" for r in bad] + p["problems"]))
            for r in bad:
                tail = [l for l in r["tail"].strip().splitlines() if l.strip()][-3:]
                lines += [f"    | {l[:200]}" for l in tail]
    return "\n".join(lines) or "Nothing is known to be broken (as of the last health checks)."


def format_attention() -> str:
    lines = []
    for p in active():
        reasons = attention(p)
        if reasons:
            lines.append(f"- {p['name']}: " + "; ".join(reasons))
    return "\n".join(lines) or "All active projects look fine."
