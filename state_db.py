"""
state_db.py — read-only reader for the Hermes session transcript store.

Athena's original backend read session JSONL files from ``<home>/webui/sessions``
and ``<home>/sessions``. Those directories are EMPTY on a real install: Hermes
persists transcripts in SQLite (``<home>/state.db``), tables ``sessions`` and
``messages``. With JSONL as the source, ``/index`` always returned an empty list
and the pane said "No artifacts yet" forever.

Everything here is read-only:

* the connection is opened ``mode=ro`` so a write can never corrupt the live DB,
* no ``journal_mode`` / ``locking_mode`` pragmas are set (those would need a
  writable handle),
* every query is wrapped so a lock, a schema change or a missing table degrades
  to "no data" instead of raising into the dashboard.

SCHEMA WE RELY ON (verified against the live 1.9 GB DB)
-------------------------------------------------------
``sessions(id, source, display_name, cwd, started_at, last_activity_at, title)``
``messages(id, session_id, role, content, tool_calls, tool_name, timestamp)``

``messages.tool_calls`` holds OpenAI-style function calls::

    [{"function": {"name": "write_file", "arguments": "{\\"path\\": \\"D:\\\\\\\\x\\\\\\\\y\\"}"}}]

and the *arguments* are a JSON STRING, so they must be decoded twice.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterator

DB_FILENAME = "state.db"

# Roles that carry tool activity. Assistant rows hold the CALL (arguments),
# tool rows hold the RESULT (which usually repeats the resolved path).
_ROLE_TOOL = "tool"
_ROLE_ASSISTANT = "assistant"

# A tool only counts as a file PRODUCER when its name says so. Matching on
# argument keys alone wrongly promoted read-only tools (read_file, grep, view).
_MUTATING_TOOL_RE = re.compile(
    r"(write|patch|edit|create|save|apply|insert|replace|append|mkdir|touch|"
    r"generate|export|render|convert|copy|move|rename|download|extract|ocr)",
    re.IGNORECASE,
)

# Tasklist tools have no file behind them; they get a synthesized virtual entry.
_TASKLIST_TOOL_RE = re.compile(
    r"(^todo$|todo_list|plan_create|plan_update|plan_todo|task_plan|update_plan|plan_follow)",
    re.IGNORECASE,
)

# Argument keys that name a single output path.
_PATH_ARG_KEYS = (
    "path",
    "file_path",
    "filepath",
    "filename",
    "file",
    "target",
    "resolved_path",
    "output_path",
    "out",
    "destination",
)
# Keys whose value is a LIST of paths.
_LISTY_ARG_KEYS = ("paths", "files", "file_paths", "targets", "file_list")
# Keys in a tool RESULT payload that name a written file.
_RESULT_PATH_KEYS = ("resolved_path", "files_modified", "path", "file", "files", "created")

PLAN_URI_PREFIX = "plan://"


# ── locating the DB ──────────────────────────────────────────────────────────
def hermes_home() -> Path:
    """The active Hermes home.

    ``ATHENA_HERMES_HOME`` wins (used by the self-test and by a profile-scoped
    backend), then ``HERMES_HOME``, then the plugin's own location
    ``<home>/plugins/athena`` walked up two levels.
    """
    for var in ("ATHENA_HERMES_HOME", "HERMES_HOME"):
        raw = os.environ.get(var)
        if raw:
            try:
                return Path(raw).resolve()
            except (TypeError, ValueError, OSError):
                continue
    here = Path(__file__).resolve()
    # .../<home>/plugins/athena/state_db.py -> <home>
    for parent in here.parents:
        if (parent / "plugins").is_dir() and (parent / DB_FILENAME).exists():
            return parent
    return here.parents[2] if len(here.parents) > 2 else here.parent


def db_path(home: Path | None = None) -> Path:
    return (home or hermes_home()) / DB_FILENAME


def _connect(path: Path) -> sqlite3.Connection:
    """Open read-only. A missing/unreadable DB is the caller's problem to handle."""
    uri = f"file:{path.as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=2.0)


def _has_schema(con: sqlite3.Connection) -> bool:
    try:
        names = {
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('sessions','messages')"
            )
        }
        return {"sessions", "messages"}.issubset(names)
    except Exception:
        return False


# ── tolerant JSON helpers ───────────────────────────────────────────────────
def _loads(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _walk_strings(node: Any, out: list[str], depth: int = 0) -> None:
    """Collect every string that LOOKS like a Windows/absolute path.

    Recursive so it works on tool result payloads like
    ``{"files_modified": ["D:\\\\a.md", "D:\\\\b.py"]}`` and on a bare list.
    """
    if depth > 6 or len(out) > 400:
        return
    if isinstance(node, str):
        if _looks_like_path(node):
            out.append(node.strip().strip('"').strip("'"))
        return
    if isinstance(node, list):
        for item in node:
            _walk_strings(item, out, depth + 1)
        return
    if isinstance(node, dict):
        for value in node.values():
            _walk_strings(value, out, depth + 1)


_PATH_SHAPE_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|/|\\\\)")


def _looks_like_path(value: str) -> bool:
    """Conservative: absolute-ish AND has a real extension."""
    if not value or len(value) > 1024:
        return False
    text = value.strip()
    if not text or "\x00" in text or "\n" in text:
        return False
    if not _PATH_SHAPE_RE.match(text):
        return False
    # A repr-escape glued inside a path means this is escaped text, not a path.
    if re.search(r"\\[nrt]\.", text):
        return False
    return bool(Path(text).suffix) and len(Path(text).suffix) <= 12


# ── tasklist synthesis ──────────────────────────────────────────────────────
_STATUS_ORDER = ("completed", "in_progress", "blocked", "pending")


def _status_of(item: dict) -> str:
    raw = str(item.get("status") or item.get("state") or "pending").strip().lower()
    if "done" in raw or "complete" in raw:
        return "completed"
    if "progress" in raw or "active" in raw or "doing" in raw:
        return "in_progress"
    if "block" in raw or "skip" in raw:
        return "blocked"
    return "pending" if raw not in _STATUS_ORDER else raw


def _task_items(args: dict) -> list[dict]:
    """Normalize every observed tasklist arg shape into a flat list of tasks.

    Observed in the live DB:
      * ``{"todos": {"item": [...]}}``          (todo_list)
      * ``{"todos": [...]}``                    (todo)
      * ``{"tasks": "[{\\"id\\": ...}]"}``       (a JSON STRING inside tasks)
      * ``{"merge": true, "todos": [...]}``
    """
    candidates: list[Any] = []
    for key in ("todos", "tasks", "items", "steps", "plan"):
        value = args.get(key)
        decoded = _loads(value)
        if decoded is None:
            continue
        if isinstance(decoded, dict):
            # {"item": [...]} — the shape todo_list emits.
            inner = decoded.get("item")
            if isinstance(inner, list):
                candidates.extend(inner)
            elif inner is not None:
                candidates.append(inner)
            else:
                for v in decoded.values():
                    if isinstance(v, list):
                        candidates.extend(v)
                    else:
                        candidates.append(v)
        elif isinstance(decoded, list):
            candidates.extend(decoded)
        else:
            candidates.append(decoded)

    tasks: list[dict] = []
    for item in candidates:
        if isinstance(item, str):
            text = item.strip()
            if text:
                tasks.append({"id": "", "label": text, "status": "pending"})
            continue
        if not isinstance(item, dict):
            continue
        label = ""
        for key in ("content", "title", "text", "task", "name", "label", "description"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                label = value.strip()
                break
        if not label:
            continue
        tasks.append(
            {
                "id": str(item.get("id") or ""),
                "label": label[:400],
                "status": _status_of(item),
            }
        )
    return tasks


def _plan_title(args: dict, tasks: list[dict]) -> str:
    for key in ("goal", "title", "name", "summary", "objective", "plan_name"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:160]
    if tasks:
        return tasks[0]["label"][:160]
    return "Session tasklist"


def synthesize_plan(title: str, tasks: list[dict], created_at: float, tool: str) -> dict:
    """A virtual artifact for a tasklist with no file on disk."""
    done = sum(1 for t in tasks if t["status"] == "completed")
    return {
        "path": f"{PLAN_URI_PREFIX}{title}",
        "label": title,
        "kind": "plan",
        "group": "Plans",
        "icon": "clipboard",
        "ext": "",
        "tool": tool,
        "source": "tasklist",
        "created_at": float(created_at or 0.0),
        "size_bytes": 0,
        "exists": True,
        "readable": True,
        "pending": False,
        "virtual": True,
        "task_count": len(tasks),
        "task_done": done,
        "tasks": tasks,
    }


# ── the main read ───────────────────────────────────────────────────────────
def iter_tool_activity(session_id: str, home: Path | None = None) -> Iterator[dict]:
    """Yield ``{"tool", "args", "created_at"}`` for one session, newest last.

    Reads the ASSISTANT rows for the CALL arguments (authoritative) and the TOOL
    rows for the RESULT paths (a file written by execute_code shows up only in
    the result). Both are needed: a subagent's write may never appear as a
    first-class call.
    """
    path = db_path(home)
    if not session_id or not path.is_file():
        return
    try:
        con = _connect(path)
    except Exception:
        return
    try:
        if not _has_schema(con):
            return
        cur = con.cursor()
        rows = cur.execute(
            "SELECT role, tool_name, content, tool_calls, timestamp "
            "FROM messages WHERE session_id = ? ORDER BY id ASC",
            (str(session_id),),
        ).fetchall()

        for role, tool_name, content, tool_calls, ts in rows:
            created = float(ts or 0.0)
            # 1) the CALL — arguments carry the real path / task list.
            for call in _iter_function_calls(tool_calls):
                name = str(call.get("name") or "")
                if not name:
                    continue
                args = _loads(call.get("arguments"))
                if not isinstance(args, dict):
                    args = {}
                yield {"tool": name, "args": args, "created_at": created, "source": "call"}
            # 2) the RESULT — catches paths written by execute_code / scripts.
            if role == _ROLE_TOOL and tool_name:
                payload = _loads(content)
                paths: list[str] = []
                if isinstance(payload, (dict, list)):
                    for key in _RESULT_PATH_KEYS:
                        if isinstance(payload, dict) and key in payload:
                            _walk_strings(payload[key], paths)
                if not paths and isinstance(content, str):
                    for m in re.finditer(r"(?:[A-Za-z]:[\\/]|/)[^\s\"'<>|*?]{0,300}?\.[A-Za-z0-9]{1,8}\b", content):
                        paths.append(m.group(0))
                if paths:
                    yield {"tool": tool_name, "args": {"__result_paths__": paths}, "created_at": created, "source": "result"}
    except Exception:
        return
    finally:
        try:
            con.close()
        except Exception:
            pass


def _iter_function_calls(tool_calls: Any) -> Iterator[dict]:
    """Normalize every observed tool_calls encoding to ``{"name", "arguments"}``."""
    parsed = _loads(tool_calls)
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        fn = entry.get("function")
        if isinstance(fn, dict):
            name = fn.get("name")
            args = fn.get("arguments")
        else:
            name = entry.get("name") or entry.get("tool") or entry.get("tool_name")
            args = entry.get("arguments") if "arguments" in entry else entry.get("input")
        if not name:
            continue
        yield {"name": str(name), "arguments": args}


def list_sessions(home: Path | None = None, limit: int = 25) -> list[dict]:
    """Most recently active sessions, newest first."""
    path = db_path(home)
    if not path.is_file():
        return []
    try:
        con = _connect(path)
    except Exception:
        return []
    try:
        if not _has_schema(con):
            return []
        rows = con.cursor().execute(
            "SELECT id, source, display_name, title, cwd, "
            "COALESCE(last_activity_at, started_at) AS act "
            "FROM sessions "
            "ORDER BY act DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [
            {
                "session_id": r[0],
                "source": r[1],
                "display_name": r[2],
                "title": r[3],
                "cwd": r[4],
                "last_activity_at": float(r[5] or 0.0),
            }
            for r in rows
        ]
    except Exception:
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass


def latest_tasklist(session_id: str, home: Path | None = None) -> dict | None:
    """The most recent synthesized tasklist for a session.

    Returns ``None`` when there is none. Used by ``/file`` so a ``plan://`` entry
    is readable, and by the pane to show live per-task status.
    """
    latest: dict | None = None
    for activity in iter_tool_activity(session_id, home):
        if not _TASKLIST_TOOL_RE.search(activity.get("tool") or ""):
            continue
        args = activity.get("args")
        if not isinstance(args, dict):
            continue
        tasks = _task_items(args)
        title = _plan_title(args, tasks)
        latest = synthesize_plan(title, tasks, activity.get("created_at") or 0.0, activity.get("tool") or "")
    return latest
