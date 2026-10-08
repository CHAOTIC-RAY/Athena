"""Athena — shared core.

Pure, importable-by-path helpers used by BOTH halves of the plugin:

* the dashboard half (``dashboard/plugin_api.py``), which runs inside the
  Hermes web server process and therefore may touch the session DB, and
* ``selftest.py``, which exercises everything offline.

Design rule for Athena: **no hook dependency**.  The reference plugin in this
data dir (``artifact-tab``) declared ``provides_hooks`` but the runtime reported
``Listens: (none)`` — nothing was ever recorded, so its pane had no data source
at all.  Athena instead DERIVES the artifact list from the session transcript
that Hermes already persists, and (optionally) from a bounded scan of the
session's own workspace directory.  Both are read-only and neither can silently
stop working because a hook failed to register.

Nothing in this module raises for bad input: every public function is total and
degrades to an empty/neutral value, because its callers are request handlers.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable, Optional

# ── limits ──────────────────────────────────────────────────────────────────
MAX_ARTIFACTS = 400
MAX_MESSAGES = 800
MAX_TEXT_BYTES = 512 * 1024          # matches Hermes' own fs preview cap
MAX_SCAN_ENTRIES = 4000              # files inspected during a workspace scan
MAX_SCAN_DEPTH = 4
SCAN_WINDOW_DAYS = 14
SCAN_RESULT_CAP = 200

# ── extension → (kind, icon) ────────────────────────────────────────────────
# `kind` is the coarse family; `group` (below) is the section the UI shows.
EXT_KIND: dict[str, tuple[str, str]] = {
    # prose / documents
    ".md": ("markdown", "\U0001f4dd"),
    ".markdown": ("markdown", "\U0001f4dd"),
    ".mdx": ("markdown", "\U0001f4dd"),
    ".txt": ("markdown", "\U0001f4c4"),
    ".rst": ("markdown", "\U0001f4c4"),
    ".adoc": ("markdown", "\U0001f4c4"),
    # html
    ".html": ("html", "\U0001f310"),
    ".htm": ("html", "\U0001f310"),
    # office / ebooks
    ".pdf": ("doc", "\U0001f4d5"),
    ".doc": ("doc", "\U0001f4d8"),
    ".docx": ("doc", "\U0001f4d8"),
    ".rtf": ("doc", "\U0001f4d8"),
    ".odt": ("doc", "\U0001f4d8"),
    ".pptx": ("doc", "\U0001f4ca"),
    ".ppt": ("doc", "\U0001f4ca"),
    ".epub": ("doc", "\U0001f4d6"),
    # sheets
    ".csv": ("sheet", "\U0001f4ca"),
    ".tsv": ("sheet", "\U0001f4ca"),
    ".xlsx": ("sheet", "\U0001f4ca"),
    ".xls": ("sheet", "\U0001f4ca"),
    # data / config
    ".json": ("data", "\U0001f9fe"),
    ".jsonl": ("data", "\U0001f9fe"),
    ".ndjson": ("data", "\U0001f9fe"),
    ".yaml": ("data", "\U0001f9fe"),
    ".yml": ("data", "\U0001f9fe"),
    ".toml": ("data", "\U0001f9fe"),
    ".xml": ("data", "\U0001f9fe"),
    ".ini": ("data", "\U0001f9fe"),
    ".cfg": ("data", "\U0001f9fe"),
    ".env": ("data", "\U0001f9fe"),
    ".sql": ("data", "\U0001f5c3"),
    ".db": ("data", "\U0001f5c3"),
    ".sqlite": ("data", "\U0001f5c3"),
    # code
    ".py": ("code", "\U0001f40d"),
    ".pyi": ("code", "\U0001f40d"),
    ".js": ("code", "\U0001f7e8"),
    ".mjs": ("code", "\U0001f7e8"),
    ".cjs": ("code", "\U0001f7e8"),
    ".jsx": ("code", "\U00002643"),
    ".ts": ("code", "\U0001f535"),
    ".tsx": ("code", "\U00002643"),
    ".go": ("code", "\U0001f439"),
    ".rs": ("code", "\U0001f980"),
    ".java": ("code", "\U00002615"),
    ".kt": ("code", "\U0001f7e3"),
    ".c": ("code", "\U0001f7e6"),
    ".h": ("code", "\U0001f7e6"),
    ".hpp": ("code", "\U0001f7e6"),
    ".cpp": ("code", "\U0001f7e6"),
    ".cc": ("code", "\U0001f7e6"),
    ".cs": ("code", "\U0001f7e9"),
    ".rb": ("code", "\U0001f48e"),
    ".php": ("code", "\U0001f418"),
    ".swift": ("code", "\U0001f426"),
    ".sh": ("code", "\U0001f41a"),
    ".bash": ("code", "\U0001f41a"),
    ".zsh": ("code", "\U0001f41a"),
    ".ps1": ("code", "\U0001f41a"),
    ".psm1": ("code", "\U0001f41a"),
    ".bat": ("code", "\U0001f41a"),
    ".cmd": ("code", "\U0001f41a"),
    ".css": ("code", "\U0001f3a8"),
    ".scss": ("code", "\U0001f3a8"),
    ".less": ("code", "\U0001f3a8"),
    ".vue": ("code", "\U0001f7e9"),
    ".svelte": ("code", "\U0001f7e0"),
    ".lua": ("code", "\U0001f319"),
    ".pl": ("code", "\U0001f42a"),
    ".r": ("code", "\U0001f4ca"),
    ".jl": ("code", "\U0001f7e3"),
    ".dart": ("code", "\U0001f3af"),
    ".ipynb": ("code", "\U0001f4d3"),
    # images
    ".png": ("image", "\U0001f5bc"),
    ".jpg": ("image", "\U0001f5bc"),
    ".jpeg": ("image", "\U0001f5bc"),
    ".gif": ("image", "\U0001f5bc"),
    ".webp": ("image", "\U0001f5bc"),
    ".bmp": ("image", "\U0001f5bc"),
    ".ico": ("image", "\U0001f5bc"),
    ".tif": ("image", "\U0001f5bc"),
    ".tiff": ("image", "\U0001f5bc"),
    ".heic": ("image", "\U0001f5bc"),
    ".avif": ("image", "\U0001f5bc"),
    ".svg": ("image", "\U0001f5bc"),
    # media
    ".mp3": ("media", "\U0001f3b5"),
    ".wav": ("media", "\U0001f3b5"),
    ".m4a": ("media", "\U0001f3b5"),
    ".ogg": ("media", "\U0001f3b5"),
    ".flac": ("media", "\U0001f3b5"),
    ".mp4": ("media", "\U0001f3ac"),
    ".mov": ("media", "\U0001f3ac"),
    ".webm": ("media", "\U0001f3ac"),
    ".mkv": ("media", "\U0001f3ac"),
    ".avi": ("media", "\U0001f3ac"),
    # archives
    ".zip": ("archive", "\U0001f5dc"),
    ".tar": ("archive", "\U0001f5dc"),
    ".gz": ("archive", "\U0001f5dc"),
    ".7z": ("archive", "\U0001f5dc"),
    ".rar": ("archive", "\U0001f5dc"),
}

IMAGE_EXTS = {e for e, (k, _) in EXT_KIND.items() if k == "image"}
TEXTY_KINDS = {"markdown", "code", "data", "html", "sheet"}

# Containers we never try to decode as text even though their headers may be
# mostly ASCII (a PDF or a zip would otherwise render as mojibake).
BINARY_EXTS = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".odt", ".epub",
    ".zip", ".tar", ".gz", ".7z", ".rar", ".db", ".sqlite", ".ico",
    ".mp3", ".wav", ".m4a", ".ogg", ".flac", ".mp4", ".mov", ".webm", ".mkv", ".avi",
}

# ── walkthrough heuristics ──────────────────────────────────────────────────
# The user's own words for what they want Athena to surface: "md doc walkthrough
# etc".  A markdown file whose NAME reads like a procedure is grouped as a
# walkthrough rather than a plain doc; a first heading that reads like one is
# accepted as a secondary signal.
_WALKTHROUGH_NAME_RE = re.compile(
    r"(walk[-_ ]?through|walkthru|guide|tutorial|how[-_ ]?to|readme|getting[-_ ]?started|"
    r"setup|install|runbook|playbook|recipe|instructions?|steps?|onboarding|quickstart|"
    r"handbook|manual|cheat[-_ ]?sheet|checklist)",
    re.IGNORECASE,
)
_WALKTHROUGH_HEAD_RE = re.compile(
    r"^\s{0,3}#{1,3}\s*(walkthrough|walk-through|guide|tutorial|how to|getting started|"
    r"setup|installation|runbook|step[- ]by[- ]step|instructions?|overview|table of contents)\b",
    re.IGNORECASE | re.MULTILINE,
)

# A markdown file that IS a plan. Without this a freshly written plan lands in
# "Docs" and the pane looks like it missed it. Two independent signals, because
# either alone misfires: a name like "plan.md" may be a database migration plan,
# and a checklist may just be a to-do note.
_PLAN_NAME_RE = re.compile(
    r"(^|[-_ .])(plan|plans|roadmap|proposal|rfc|spec|design|implementation[-_ ]?plan|"
    r"task[-_ ]?list|todo)s?([-_ .]|$)",
    re.IGNORECASE,
)
# A GFM task list plus a "goal"-ish heading is the shape of a plan body.
_PLAN_HEAD_RE = re.compile(
    r"^\s{0,3}#{1,3}\s*(goal|objective|overview|scope|milestones?|phases?|deliverables?)\b",
    re.IGNORECASE | re.MULTILINE,
)
_PLAN_CHECKBOX_RE = re.compile(r"^\s*[-*]\s*\[[ xX]\]", re.MULTILINE)


def _is_plan_like(name: str, head: Optional[str] = None) -> bool:
    """True when a markdown file reads as a plan/task document."""
    if _PLAN_NAME_RE.search(str(name or "")):
        return True
    if not head:
        return False
    text = head[:4000]
    checkboxes = len(_PLAN_CHECKBOX_RE.findall(text))
    if checkboxes >= 2 and _PLAN_HEAD_RE.search(text):
        return True
    # Many task boxes on their own is a plan even without a matching heading.
    return checkboxes >= 5

# A markdown file can be a plan by NAME (plan.md, spec.md, TODO-LIST.md) or by
# BODY — a checklist plus a goal/scope heading.  Name alone is too narrow (a plan
# is often just "notes.md"); body alone is too loose (any bulleted list looks
# like one).  Requiring both signals keeps ordinary docs in Docs.
_PLAN_NAME_RE = re.compile(
    r'(^|[-_ .])(plan|plans|roadmap|proposal|rfc|spec|design|implementation[-_ ]?plan|task[-_ ]?list|todo)s?([-_ .]|$)',
    re.IGNORECASE,
)
_PLAN_HEAD_RE = re.compile(
    r'^\s{0,3}#{1,3}\s*(goal|objective|overview|scope|milestones?|phases?|deliverables?)\b',
    re.IGNORECASE | re.MULTILINE,
)
_PLAN_CHECKBOX_RE = re.compile(r'^\s*[-*]\s*\[[ xX]\]', re.MULTILINE)

PLAN_PATH_PREFIX = "plan://"

# ── sensitive paths ─────────────────────────────────────────────────────────
# Athena can render file text in the UI, so it refuses credential-shaped files
# outright rather than relying on the caller to be careful.
_DENY_NAME_RE = re.compile(
    r"^(\.env(\..*)?|\.envrc|id_rsa|id_dsa|id_ecdsa|id_ed25519|"
    r"\.credentials\.yaml|credentials(\.json)?|auth\.json|"
    r"\.netrc|\.pgpass|\.npmrc|\.pypirc|secrets?(\.(json|ya?ml|txt|ini|toml))?|"
    # Tool-agnostic credential stores found by an adversarial probe of this file:
    # each of these returned 200 with live secret material before the deny list
    # was widened.
    r"\.git-credentials|\.docker/config\.json|"
    r"kubeconfig|\.kube/config|"
    r"azure\.json|\.azure/(accessTokens|refreshTokens)?|"
    r"token\.json|tokens?\.json|gh_token\.txt|"
    r"my\.credentials\.yaml|"
    r"[\w.-]*\.(pem|key|p12|pfx|keystore|jks|ppk|ovpn|asc|gpg|kdbx))$",
    re.IGNORECASE,
)
_DENY_DIR_PARTS = {
    ".ssh", ".aws", ".gnupg", ".azure", ".kube", "mcp-tokens", "vault",
    ".docker", ".gnucash", ".password-store",
}



def _is_plan_like(name: str, head: str) -> bool:
    """True when a markdown file reads as a plan/task document."""
    if _PLAN_NAME_RE.search(str(name)):
        return True
    if not head:
        return False
    text = head[:4000]
    checkboxes = _PLAN_CHECKBOX_RE.findall(text)
    if len(checkboxes) >= 2 and _PLAN_HEAD_RE.search(text):
        return True
    return len(checkboxes) >= 5

def norm_key(path: str) -> str:
    """Case-folded, separator-normalised key for dedupe on Windows."""
    return os.path.normcase(os.path.normpath(str(path)))


def is_denied(path: str) -> bool:
    """True when a path looks credential-bearing and must never be served."""
    try:
        p = Path(str(path))
    except (TypeError, ValueError, OSError):
        return True
    if _DENY_NAME_RE.match(p.name):
        return True
    lowered = {part.lower() for part in p.parts}
    return bool(lowered & _DENY_DIR_PARTS)


def ext_of(path: str) -> str:
    try:
        return Path(str(path)).suffix.lower()
    except (TypeError, ValueError, OSError):
        return ""


def classify(path: str, *, head: Optional[str] = None, is_plan: bool = False) -> dict[str, str]:
    """Map a path (plus optional content head) to kind/group/icon/label."""
    if is_plan or str(path).startswith(PLAN_PATH_PREFIX):
        return {"kind": "plan", "group": "Plans", "icon": "\U0001f5c2", "ext": ""}

    ext = ext_of(path)
    kind, icon = EXT_KIND.get(ext, ("other", "\U0001f4c1"))

    name = Path(str(path)).name
    group = {
        "markdown": "Docs",
        "html": "Docs",
        "doc": "Docs",
        "code": "Code",
        "data": "Data",
        "sheet": "Data",
        "image": "Media",
        "media": "Media",
        "archive": "Other",
        "other": "Other",
    }.get(kind, "Other")

    # Promote procedural markdown to the Walkthroughs section.
    if kind == "markdown":
        if _WALKTHROUGH_NAME_RE.search(name):
            group = "Walkthroughs"
        elif head and _WALKTHROUGH_HEAD_RE.search(head[:4000]):
            group = "Walkthroughs"

    # A markdown file that IS a plan belongs in Plans, not Docs. Without this the
    # pane buries a freshly written plan under Docs, so "did Athena pick it up?"
    # reads as no. Detected from the filename or the checklist-shaped body.
    if kind == "markdown" and group not in {"Walkthroughs"} and _is_plan_like(name, head):
        group = "Plans"
        icon = "\U0001f5c2"

    return {"kind": kind, "group": group, "icon": icon, "ext": ext}


# ── path extraction from the transcript ─────────────────────────────────────
# A tool counts as a producer only when its NAME says so. An earlier revision
# also accepted "the args contain a `path` key", which wrongly promoted read-only
# tools (`read_file`, `grep`, `view`) into artifacts — the self-test caught it.
_MUTATING_TOOL_RE = re.compile(
    r"(write|patch|edit|create|save|apply|insert|replace|append|mkdir|touch|"
    r"generate|export|render|convert|copy|move|rename|download|extract|ocr)",
    re.IGNORECASE,
)
# A written tasklist is an artifact with no file behind it; the reference plugin
# synthesized a `plan://` entry for it and so does Athena.
_PLAN_TOOL_RE = re.compile(r"(todo|task_plan|update_plan|plan_follow|^plan$)", re.IGNORECASE)
# Keys whose value is an output the tool has not necessarily created yet.
_PENDING_KEYS = {"output_path", "out"}
_PATH_ARG_KEYS = ("path", "file_path", "filepath", "filename", "file", "target", "output_path", "out")
_LISTY_ARG_KEYS = ("paths", "files", "file_paths", "targets")
_MEDIA_MARKER_RE = re.compile(r"MEDIA:\s*([^\r\n]+)")
# Any absolute-ish path with a known extension, used as a last resort over
# result text.  Requires a drive letter / leading slash so bare words can't match.
_ABS_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/]|/)[^\s\"'<>|*?\r\n]{0,400}?\.[A-Za-z0-9]{1,8}\b"
)
# A literal `\n`/`\t`/`\r` escape followed by a dot: a repr artifact, not a path.
_ESCAPE_GLUE_RE = re.compile(r"\\[nrt]\.")
# Result text mentions paths that belong to the toolchain, not to the session:
# dependency caches, virtualenvs, kernels, OS temp. Those are not "artifacts
# this session produced", so they never reach the panel.
_NOISE_DIR_RE = re.compile(
    r"(site-packages|node_modules|__pycache__|\\uv\\cache|\\pip\\cache|"
    r"\\AppData\\Local\\Temp\\|\\AppData\\Local\\hermes\\cache|hermes_kernel_|"
    r"\\Windows\\|\\Program Files|\\ProgramData\\|\.git\\|\.venv\\|\\venv\\|"
    r"\\dist\\|\\build\\|\\\.next\\|\\\.cache\\)",
    re.IGNORECASE,
)


def _is_existing_file(path: str) -> bool:
    try:
        return os.path.isfile(path)
    except (OSError, ValueError):
        return False


def _coerce_args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _candidate_paths(args: dict) -> list[tuple[str, str]]:
    """Return (value, source key) pairs so the caller knows if it is an output."""
    out: list[tuple[str, str]] = []
    for key in _PATH_ARG_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            out.append((value.strip(), key))
    for key in _LISTY_ARG_KEYS:
        value = args.get(key)
        if isinstance(value, list):
            out.extend((str(v).strip(), key) for v in value if isinstance(v, str) and v.strip())
    return out


def _plan_title(args: dict) -> tuple[str, int]:
    """Derive a title and task count from a tasklist tool's arguments."""
    title = ""
    for key in ("title", "goal", "summary", "name", "objective"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            title = value.strip()
            break

    tasks = None
    for key in ("tasks", "todos", "items", "steps"):
        value = args.get(key)
        if isinstance(value, list):
            tasks = value
            break
    count = 0
    if tasks is not None:
        count = len(tasks)
        if not title and tasks:
            first = tasks[0]
            if isinstance(first, str):
                title = first.strip()
            elif isinstance(first, dict):
                for key in ("content", "text", "task", "title", "name"):
                    value = first.get(key)
                    if isinstance(value, str) and value.strip():
                        title = value.strip()
                        break
    if not title:
        title = "Session tasklist"
    return title[:160], count


def _looks_like_path(value: str) -> bool:
    if not value or len(value) > 1024:
        return False
    if "\n" in value or "\x00" in value:
        return False
    # Tool output often prints a Python/JS repr, so a literal `\n` escape can sit
    # inside what otherwise looks like a path and glue several lines together
    # (observed: `D:\incoming\n.\logos\n.\README.md`). A real Windows directory
    # named `n` would appear as `\n\`, not `\n.`, so this is safe to reject.
    if _ESCAPE_GLUE_RE.search(value):
        return False
    return ext_of(value) in EXT_KIND


def _resolve(path: str, cwd: Optional[str]) -> Optional[str]:
    """Absolute-ise a transcript path against the session cwd."""
    try:
        raw = str(path).strip().strip('"').strip("'")
        if not raw:
            return None
        # Strip a trailing ":line:col" locator some tools append.
        raw = re.sub(r":(\d+)(?::\d+)?$", "", raw)
        p = Path(raw)
        if not p.is_absolute():
            if not cwd:
                return None
            p = Path(cwd) / p
        return os.path.normpath(str(p))
    except (TypeError, ValueError, OSError):
        return None


def extract_from_messages(
    messages: Iterable[dict], cwd: Optional[str] = None
) -> list[dict[str, Any]]:
    """Pull artifact candidates out of persisted transcript messages.

    Reads assistant ``tool_calls`` (the durable, structured signal) and falls
    back to ``MEDIA:`` markers and absolute paths embedded in tool results.
    Returns raw candidate dicts with no filesystem stat applied.
    """
    found: dict[str, dict[str, Any]] = {}
    order = 0

    def _add(raw_path: str, *, tool: str, created_at: float, pending: bool = False) -> None:
        nonlocal order
        raw = str(raw_path).strip().strip('"').strip("'")
        if not raw:
            return
        # A `plan://` entry is a synthetic address, not a filesystem path — it
        # must not go through cwd resolution (which would drop it).
        if raw.startswith(PLAN_PATH_PREFIX):
            resolved = raw
        else:
            resolved = _resolve(raw, cwd)
            if not resolved or not _looks_like_path(resolved):
                return
        key = norm_key(resolved)
        if key in found:
            return
        order += 1
        found[key] = {
            "path": resolved,
            "tool": tool,
            "created_at": created_at,
            "pending": pending,
            "order": order,
        }

    for msg in messages or ():
        if not isinstance(msg, dict):
            continue
        created = msg.get("created_at") or msg.get("timestamp") or msg.get("ts") or 0
        try:
            created_at = float(created)
        except (TypeError, ValueError):
            created_at = 0.0
        if created_at > 1e11:  # milliseconds
            created_at /= 1000.0

        # 1) structured tool calls — the authoritative signal
        for call in msg.get("tool_calls") or ():
            if not isinstance(call, dict):
                continue
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = str(fn.get("name") or call.get("name") or "")
            args = _coerce_args(fn.get("arguments", call.get("arguments")))
            if not name and not args:
                continue

            is_plan_tool = bool(_PLAN_TOOL_RE.search(name))
            if not (bool(_MUTATING_TOOL_RE.search(name)) or is_plan_tool):
                continue

            for candidate, key in _candidate_paths(args):
                _add(candidate, tool=name, created_at=created_at, pending=key in _PENDING_KEYS)

            # A tasklist is an artifact with no file behind it.
            if is_plan_tool:
                title, task_count = _plan_title(args)
                _add(f"{PLAN_PATH_PREFIX}{title}", tool=name, created_at=created_at)
                if title:
                    entry = found.get(norm_key(f"{PLAN_PATH_PREFIX}{title}"))
                    if entry is not None:
                        entry["task_count"] = task_count

        # 2) result text: MEDIA: markers, then absolute paths. Result text is
        #    noisy — it mentions the toolchain's own files — so a path found here
        #    must EXIST and must not live in a cache/vendor/temp directory.
        content = msg.get("content")
        if isinstance(content, str) and content:
            for marker in _MEDIA_MARKER_RE.findall(content):
                candidate = marker.strip()
                if _NOISE_DIR_RE.search(candidate):
                    continue
                _add(candidate, tool="media", created_at=created_at)
            if msg.get("role") == "tool":
                for hit in _ABS_PATH_RE.findall(content)[:10]:
                    if _NOISE_DIR_RE.search(hit) or not _is_existing_file(hit):
                        continue
                    _add(hit, tool="result", created_at=created_at)

    return sorted(found.values(), key=lambda item: (-item["created_at"], item["order"]))


def scan_workspace(
    cwd: Optional[str], *, window_days: int = SCAN_WINDOW_DAYS, cap: int = SCAN_RESULT_CAP
) -> list[dict[str, Any]]:
    """Bounded scan of the session workspace for recently touched artifacts.

    Optional enrichment for the case where a file predates the transcript or was
    produced by a non-mutating tool.  Depth-, count- and age-limited so it stays
    fast on a large repo.
    """
    if not cwd:
        return []
    root = Path(cwd)
    if not root.is_dir():
        return []

    cutoff = time.time() - window_days * 86400
    out: list[dict[str, Any]] = []
    inspected = 0
    skip_dirs = {
        ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
        ".next", ".cache", ".mypy_cache", ".pytest_cache", ".ruff_cache", "target",
        "site-packages", ".idea", ".vscode",
    }

    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack and inspected < MAX_SCAN_ENTRIES and len(out) < cap:
        current, depth = stack.pop()
        try:
            entries = list(os.scandir(current))
        except (OSError, PermissionError):
            continue
        for entry in entries:
            inspected += 1
            if inspected >= MAX_SCAN_ENTRIES:
                break
            try:
                if entry.is_dir(follow_symlinks=False):
                    if depth + 1 <= MAX_SCAN_DEPTH and entry.name not in skip_dirs and not entry.name.startswith("."):
                        stack.append((Path(entry.path), depth + 1))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                if ext_of(entry.name) not in EXT_KIND:
                    continue
                stat = entry.stat()
            except (OSError, PermissionError):
                continue
            if stat.st_mtime < cutoff:
                continue
            out.append(
                {
                    "path": os.path.normpath(entry.path),
                    "tool": "workspace",
                    "created_at": stat.st_mtime,
                    "pending": False,
                    "order": 0,
                    "size_bytes": stat.st_size,
                }
            )

    out.sort(key=lambda item: -(item.get("created_at") or 0))
    return out[:cap]


# ── enrichment ──────────────────────────────────────────────────────────────
def stat_one(path: str) -> dict[str, Any]:
    """Best-effort stat; never raises."""
    info: dict[str, Any] = {"exists": False, "size_bytes": 0, "mtime": 0.0, "is_dir": False}
    try:
        st = os.stat(path)
        info.update(
            exists=True,
            size_bytes=int(st.st_size),
            mtime=float(st.st_mtime),
            is_dir=os.path.isdir(path),
        )
    except (OSError, ValueError):
        pass
    return info


def enrich(candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add classification + stat + display fields, dedupe, sort, cap."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []

    for item in candidates or ():
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if not isinstance(path, str) or not path:
            continue
        key = norm_key(path)
        if key in seen:
            continue
        seen.add(key)

        is_plan = str(path).startswith(PLAN_PATH_PREFIX) or item.get("kind") == "plan"
        head = None
        if not is_plan and item.get("exists", True):
            head = _peek(path)

        cls = classify(path, head=head, is_plan=is_plan)
        info = {"exists": True, "size_bytes": 0, "mtime": 0.0, "is_dir": False}
        if not is_plan:
            info = stat_one(path)

        label = Path(str(path)).name if not is_plan else str(path)[len(PLAN_PATH_PREFIX):]
        if not label:
            label = str(path)

        out.append(
            {
                "path": path,
                "label": label,
                "kind": cls["kind"],
                "group": cls["group"],
                "icon": cls["icon"],
                "ext": cls["ext"],
                "tool": item.get("tool") or "",
                "source": item.get("source") or ("transcript" if item.get("tool") != "workspace" else "workspace"),
                "created_at": float(item.get("created_at") or 0.0),
                "size_bytes": int(info["size_bytes"] if item.get("size_bytes") is None else item.get("size_bytes") or info["size_bytes"]),
                "mtime": float(info["mtime"]),
                "exists": bool(info["exists"]) if not is_plan else True,
                "pending": bool(item.get("pending")),
                "task_count": int(item.get("task_count") or 0),
                "denied": is_denied(path),
                "readable": (not is_plan) and bool(info["exists"]) and not bool(info["is_dir"]) and not is_denied(path),
            }
        )

    # Openable files first, then files that still exist, then the rest; newest
    # first within each band. A month-old session has paths that have since
    # moved, so the panel must lead with something the user can actually open.
    out.sort(
        key=lambda item: (
            0 if item.get("readable") else (1 if item.get("exists") else 2),
            -(item.get("created_at") or 0),
            item.get("label") or "",
        )
    )
    return out[:MAX_ARTIFACTS]

def _peek(path: str) -> str:
    """Read a small head of a text file for classification; '' on any problem."""
    if is_denied(path):
        return ""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(4096)
    except (OSError, ValueError):
        return ""
    try:
        return raw.decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - decode is already total
        return ""


def group_counts(artifacts: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in artifacts or ():
        group = item.get("group") or "Other"
        counts[group] = counts.get(group, 0) + 1
    return counts


# ── safe text read ──────────────────────────────────────────────────────────
def looks_binary(raw: bytes) -> bool:
    """Decide text-vs-binary.

    Judged on the DECODED text, not raw byte values: a Thaana (Dhivehi) or any
    other non-Latin UTF-8 document is full of bytes >= 0x80 and would otherwise
    be misreported as binary.  A previous revision made exactly that mistake —
    the self-test caught it before it reached the panel.
    """
    if not raw:
        return False
    if b"\x00" in raw:
        return True

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = None

    if text is not None:
        if not text:
            return False
        controls = sum(1 for ch in text if ch < " " and ch not in "\t\n\r")
        return (controls / len(text)) > 0.05

    # Not valid UTF-8 — fall back to a byte-value heuristic.
    sample = raw[:4096]
    if not sample:
        return False
    printable = sum(1 for byte in sample if 32 <= byte < 127 or byte in (9, 10, 13))
    return (printable / len(sample)) < 0.85


LANGUAGE_BY_EXT = {
    "py": "python", "pyi": "python", "js": "javascript", "mjs": "javascript",
    "cjs": "javascript", "jsx": "jsx", "ts": "typescript", "tsx": "tsx",
    "json": "json", "jsonl": "json", "yaml": "yaml", "yml": "yaml",
    "toml": "toml", "xml": "xml", "html": "html", "htm": "html",
    "css": "css", "scss": "scss", "md": "markdown", "markdown": "markdown",
    "mdx": "markdown", "sh": "bash", "bash": "bash", "zsh": "bash",
    "ps1": "powershell", "psm1": "powershell", "bat": "batch", "cmd": "batch",
    "sql": "sql", "go": "go", "rs": "rust", "java": "java", "kt": "kotlin",
    "c": "c", "h": "c", "cpp": "cpp", "hpp": "cpp", "cc": "cpp", "cs": "csharp",
    "rb": "ruby", "php": "php", "swift": "swift", "lua": "lua", "pl": "perl",
    "r": "r", "jl": "julia", "dart": "dart", "vue": "vue", "svelte": "svelte",
    "ini": "ini", "cfg": "ini", "env": "ini", "csv": "csv", "tsv": "tsv",
    "txt": "text", "rst": "rst",
}


def language_for(path: str) -> str:
    return LANGUAGE_BY_EXT.get(ext_of(path).lstrip("."), "text")


def safe_read(path: str, *, max_bytes: int = MAX_TEXT_BYTES, allow: Optional[set[str]] = None) -> dict[str, Any]:
    """Read a file for in-panel display.

    ``allow`` — when provided, an exact set of permitted case-folded paths; the
    request is refused unless the target is in it.  Every failure path returns a
    structured result instead of raising.
    """
    result: dict[str, Any] = {
        "path": path, "ok": False, "binary": False, "truncated": False,
        "text": "", "size_bytes": 0, "language": language_for(path),
        "mimeType": "text/plain", "error": "",
    }

    if not isinstance(path, str) or not path.strip():
        result["error"] = "path is required"
        return result

    try:
        resolved = os.path.normpath(os.path.abspath(path))
    except (TypeError, ValueError, OSError):
        result["error"] = "path is not resolvable"
        return result
    result["path"] = resolved

    if allow is not None and norm_key(resolved) not in allow:
        result["error"] = "This file is not part of the current session's artifacts."
        return result
    if is_denied(resolved):
        result["error"] = "Refused: this looks like a credential file."
        return result
    if not os.path.isfile(resolved):
        result["error"] = "File not found (it may have been moved or deleted)."
        return result

    try:
        size = os.path.getsize(resolved)
    except OSError:
        size = 0
    result["size_bytes"] = size

    try:
        with open(resolved, "rb") as handle:
            raw = handle.read(max_bytes + 1)
    except (OSError, ValueError) as exc:
        result["error"] = f"Could not read the file: {type(exc).__name__}"
        return result

    if len(raw) > max_bytes:
        raw = raw[:max_bytes]
        result["truncated"] = True

    ext = ext_of(resolved).lstrip(".")
    if ext in IMAGE_EXTS or f".{ext}" in BINARY_EXTS or looks_binary(raw):
        result["binary"] = True
        result["ok"] = True
        result["mimeType"] = _mime_for(ext)
        return result

    result["text"] = raw.decode("utf-8", errors="replace")
    result["ok"] = True
    result["mimeType"] = _mime_for(ext)
    return result


_MIME = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
    "ico": "image/x-icon", "tif": "image/tiff", "tiff": "image/tiff",
    "svg": "image/svg+xml", "pdf": "application/pdf", "avif": "image/avif",
}


def _mime_for(ext: str) -> str:
    if ext in _MIME:
        return _MIME[ext]
    if ext == "md" or ext == "markdown":
        return "text/markdown"
    if ext in ("json", "jsonl", "ndjson"):
        return "application/json"
    if ext in ("html", "htm"):
        return "text/html"
    return "text/plain"
