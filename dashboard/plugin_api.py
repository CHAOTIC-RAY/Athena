"""
plugin_api.py — HTTP surface for athena.

DATA PATH
---------
Athena originally read session JSONL from ``<home>/webui/sessions`` and
``<home>/sessions``. Both are EMPTY on a real install — Hermes persists
transcripts in SQLite (``<home>/state.db``). That mismatch is why the pane read
"No artifacts yet" forever: the routes worked, the SOURCE was empty.

This module builds the index from :mod:`state_db`, which reads
``messages.tool_calls`` (the authoritative tool ARGUMENTS — real paths and real
task lists) plus ``messages.content`` for tool RESULTS (catches files written by
execute_code / subagents). The connection is read-only.

ROUTES
------
``/health``  liveness + whether the DB was found
``/index``   artifacts for a session, grouped, incl. a synthesized tasklist entry
``/file``    one artifact's content; also serves ``plan://`` virtual tasklists
``/events``  SSE mirror of /index (the pane polls; this is for curl/future use)
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Query

logger = logging.getLogger("athena")
router = APIRouter()

PLUGIN_ID = "athena"
_KB = 1024
MAX_FILE_BYTES = 2 * _KB * _KB
MAX_ARTIFACTS = 150

# Directory names whose contents are toolchain noise, never "session artifacts".
# Matched as WHOLE PATH SEGMENTS — a bare substring test wrongly dropped real
# files like ``.../distribute.md`` or ``.../build-log.txt``.
_NOISE_DIR_NAMES = {
    "site-packages", "node_modules", "__pycache__", ".git", ".venv", "venv",
    "dist", "build", ".next", ".cache", "state-snapshots", "profile-backups",
    "target", ".mypy_cache", ".pytest_cache", ".tox",
}
_NOISE_FRAGMENTS = (
    "appdata\\local\\temp", "hermes_kernel_", "windows\\system32",
    "program files", "programdata", "uv\\cache", "pip\\cache",
)
# SQLite/Gateway sidecar + runtime lockfiles are infrastructure, not artifacts.
# Without this the pane fills with ``state.db-wal`` / ``kanban.db-shm`` because
# tool RESULTS mention them constantly.
_NOISE_NAMES = {
    ".tick.lock", "auth.lock", "cleanup.log",
}
_NOISE_SUFFIXES = (
    ".db-wal", ".db-shm", ".db-journal", "-wal", "-shm", "-journal",
    ".lock", ".pid", ".tmp", ".partial", ".crdownload",
    # runtime state a tool merely READS and echoes back — not a session artifact
    ".log", ".db", ".sqlite", ".sqlite3",
)


def _is_noise(path: str, *, in_home: bool = False) -> bool:
    """True for toolchain/runtime noise. NEVER true for a ``plan://`` virtual entry.

    ``in_home`` exempts the location-fragment test. Rationale: a path that resolves
    inside the session's own Hermes home and was referenced by a tool in this
    session IS a session artifact — the outside-home check already rejects
    everything else, so the location heuristic only ever misfired (it vetoed a
    fixture written under ``AppData\\Local\\Temp`` and would veto any home that
    happens to live under a "noise-looking" directory).
    """
    low = str(path).lower().replace("/", "\\")
    if low.startswith("plan://"):
        return False
    if not in_home and any(frag in low for frag in _NOISE_FRAGMENTS):
        return True
    # Segment-wise directory match so `distribute.md` and `build-log.txt` survive.
    parts = low.split("\\")
    if any(part in _NOISE_DIR_NAMES for part in parts[:-1]):
        return True
    name = parts[-1]
    if name in _NOISE_NAMES:
        return True
    return any(name.endswith(sfx) for sfx in _NOISE_SUFFIXES)

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent


# ── load sibling modules by path (web server imports this file by its own name) ──
# Fingerprint recorded when a sibling was actually executed. `__file__` cannot be
# used for this: the cached module's `__file__` IS the source path we are about to
# stat, so comparing them only ever compares a file to itself and never detects a
# change. Keyed by module name -> (mtime_ns, size).
#
# This table MUST be process-global rather than a module attribute: Hermes
# re-imports plugin_api.py under a fresh module object on reload, and a per-module
# table would start empty and force a needless re-execute of an unchanged sibling
# on every reload. sys.modules survives the reload, so it is the right home.
_SIBLING_FINGERPRINTS: dict[str, tuple[int, int]] = sys.modules.setdefault(
    "__athena_sibling_fingerprints__", {}
)


def _fingerprint(path: Path) -> Optional[tuple[int, int]]:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _load_sibling(module_name: str, filename: str) -> Any:
    """Load a sibling module, reusing sys.modules ONLY if the file is unchanged.

    Hermes imports this file once per server start, so a cached sibling would
    otherwise pin an OLD copy for the life of the process: edit artifact_core.py,
    reload the app, and Athena keeps serving the previous behaviour. We record a
    (mtime_ns, size) fingerprint when the module is executed and re-execute it
    whenever the source on disk no longer matches that record.
    """
    path = _PLUGIN_ROOT / filename
    cached = sys.modules.get(module_name)
    current = _fingerprint(path)
    if cached is not None:
        recorded = _SIBLING_FINGERPRINTS.get(module_name)
        # No fingerprint means we never recorded how this module was built, so we
        # cannot claim it matches the file — re-execute rather than trust it.
        if recorded is not None and current is not None and recorded == current:
            return cached
        if current is None:
            # The source file is gone. Serving the already-loaded copy beats
            # raising FileNotFoundError and taking every Athena route down, so keep
            # it — but forget the fingerprint so that restoring the file is picked
            # up on the next call instead of pinning the orphan forever.
            logger.warning(
                "athena: %s is missing; continuing with the already-loaded %s",
                path, module_name,
            )
            _SIBLING_FINGERPRINTS.pop(module_name, None)
            return cached
        logger.info("athena: %s changed; reloading sibling %s", path, module_name)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        # Never leave a half-built module behind for the next caller to reuse.
        sys.modules.pop(module_name, None)
        _SIBLING_FINGERPRINTS.pop(module_name, None)
        raise
    # Record only a successful load, and record the fingerprint as of exec time.
    fingerprint = _fingerprint(path) or current
    if fingerprint is not None:
        _SIBLING_FINGERPRINTS[module_name] = fingerprint
    return module


def _state_db() -> Any:
    return _load_sibling("athena_state_db", "state_db.py")


def _artifact_core() -> Any:
    return _load_sibling("athena_artifact_core", "artifact_core.py")


def _athena_store() -> Any:
    """The live JSON store fed by Athena's ``post_tool_call`` hook."""
    return _load_sibling("athena_store", "athena_store.py")


def hermes_home() -> Path:
    return _state_db().hermes_home()


def _home() -> Path:
    try:
        return hermes_home()
    except Exception:
        return _PLUGIN_ROOT.parent.parent


def _safe_within_home(path: Path) -> bool:
    try:
        return path.resolve().is_relative_to(_home().resolve())
    except Exception:
        return False


def _is_denied(path: Any) -> bool:
    """Credential-bearing paths are never served (``.ssh``, ``id_rsa``, ...)."""
    try:
        return bool(_artifact_core().is_denied(path))
    except Exception:
        return True


def _head_of(resolved: Path, limit: int = 4000) -> str:
    """First few KB of a text file, for content-aware classification.

    Only markdown-ish files are sniffed, and only a bounded prefix, so /index
    stays cheap even though the session DB is ~1.9GB.
    """
    if resolved.suffix.lower() not in {".md", ".markdown", ".txt"}:
        return ""
    try:
        with open(resolved, "rb") as handle:
            raw = handle.read(limit)
    except OSError:
        return ""
    if b"\x00" in raw:
        return ""
    return raw.decode("utf-8", errors="replace")


def _classify(path: str, head: str = "") -> dict[str, str]:
    """Group/kind/icon, reusing artifact_core so both halves agree."""
    try:
        cls = _artifact_core().classify(path, head=head or None)
        return {
            "kind": cls.get("kind", "other"),
            "group": cls.get("group", "Other"),
            "icon": cls.get("icon", ""),
            "ext": cls.get("ext", ""),
        }
    except Exception:
        return {
            "kind": "other",
            "group": "Other",
            "icon": "",
            "ext": Path(path).suffix.lower(),
        }


def _describe(path: str, tool: str, created_at: float, source: str) -> dict | None:
    """Build one artifact entry, or None when it must not be listed."""
    if not path:
        return None
    raw = str(path).strip().strip('"').strip("'")
    if not raw:
        return None
    p = Path(raw)
    try:
        resolved = p.resolve()
    except Exception:
        return None
    if not _safe_within_home(resolved):
        return None
    in_home = True
    if _is_noise(raw, in_home=in_home):
        return None
    # Credential paths are dropped from the index ENTIRELY. Listing them even as
    # non-openable leaks the existence and filename of e.g. ``~/.ssh/id_rsa``.
    if _is_denied(resolved):
        return None
    exists = False
    size = 0
    try:
        exists = resolved.is_file()
        if exists:
            size = resolved.stat().st_size
    except Exception:
        exists = False
    # Sniff only real, small-enough files: a plan is recognised by its body when
    # the filename is unremarkable.
    head = _head_of(resolved) if (exists and size <= 512 * 1024) else ""
    cls = _classify(raw, head)
    return {
        "path": raw,
        "label": p.name or raw,
        "kind": cls["kind"],
        "group": cls["group"],
        "icon": cls["icon"],
        "ext": cls["ext"],
        "tool": tool or "",
        "source": source or "transcript",
        "created_at": float(created_at or 0.0),
        "size_bytes": size,
        "exists": exists,
        "readable": exists,
        "denied": False,
        "pending": not exists,
        "virtual": False,
    }


def _dedupe_key(raw: str) -> str:
    """Identity of a path for dedupe, resolved first.

    Plain ``normcase(normpath(...))`` is not enough on Windows: the SAME file can
    arrive as a long name (``C:/Users/marketing/...``) from one source and as an
    8.3 short name (``C:/Users/MARKET~1/...``) from another, which normpath keeps
    distinct and so produces a duplicate row. Resolving first collapses that to
    one identity. Non-resolvable paths fall back to the textual form.
    """
    try:
        return os.path.normcase(os.path.normpath(str(Path(str(raw)).resolve())))
    except Exception:
        try:
            return os.path.normcase(os.path.normpath(str(raw)))
        except Exception:
            return str(raw)


def _recorded_rows(session_id: str) -> list[dict]:
    """Raw store rows visible to one session, BEFORE any gate. Never raises.

    Session scoping: a row recorded WITH a session id is offered only to that
    session. A row with no session id is unattributed and is offered to every
    session, which is what lets the store backfill sessions whose writes the hook
    never observed.
    """
    try:
        store = _athena_store()
        snap = store.snapshot()
        rows = snap.get("items") if isinstance(snap, dict) else None
        epoch_of = store.epoch_of
    except Exception:
        return []
    if not isinstance(rows, list):
        return []

    wanted = str(session_id or "")
    out: list[tuple[dict, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        owner = str(row.get("session_id") or "")
        if owner and wanted and owner != wanted:
            continue
        out.append((row, epoch_of(row.get("at") or "")))
    return out


def _recorded_items(session_id: str) -> list[dict]:
    """Artifacts the ``post_tool_call`` hook recorded, via the existing gates.

    Each recorded path is pushed through :func:`_describe`, so hook-sourced items
    get EXACTLY the same treatment as transcript-sourced ones: must resolve
    inside the Hermes home, must clear the noise filter, and must not be denied
    by ``artifact_core.is_denied``. There is deliberately no second, looser gate
    here — a denied or out-of-home path recorded by the hook is dropped.

    Used by tests and callers that want the gated view only; :func:`build_index`
    works from :func:`_recorded_rows` directly to avoid loading the store twice.
    """
    out: list[dict] = []
    for row, epoch in _recorded_rows(session_id):
        path = row.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        entry = _describe(
            path.strip(),
            str(row.get("tool") or "hook"),
            epoch,
            "hook",
        )
        if entry:
            out.append(entry)
    return out


def build_index(session_id: str, scan: bool = False) -> dict[str, Any]:
    """The artifact index for one session, grouped, newest first within a band."""
    db = _state_db()
    home = _home()
    artifacts: list[dict] = []
    seen: set[str] = set()

    # 0) LIVE hook-recorded artifacts.
    #
    # Added FIRST and unconditionally, because it is the fresher source: the
    # hook observed the write as it happened, whereas the transcript row may be
    # absent, stale, or scoped to a different session id. The `seen` gate below
    # makes the hook item win the dedupe, and the transcript backfill then fills
    # in whatever the hook never saw (older sessions, writes made before this
    # plugin was installed).
    recorded = _recorded_rows(session_id)
    hook_seen = 0
    hook_listed = 0
    hook_refused = 0
    for row, epoch in recorded:
        hook_seen += 1
        if len(artifacts) >= MAX_ARTIFACTS:
            hook_refused += 1
            continue
        path = row.get("path")
        if not isinstance(path, str) or not path.strip():
            hook_refused += 1
            continue
        entry = _describe(path.strip(), str(row.get("tool") or "hook"), epoch, "hook")
        if not entry:
            # Refused by containment / denial / noise — count it, then drop it.
            hook_refused += 1
            continue
        key = _dedupe_key(entry.get("path") or "")
        if key in seen:
            # Already present from an earlier source; the row was seen, not refused.
            hook_listed += 1
            continue
        seen.add(key)
        hook_listed += 1
        artifacts.append(entry)

    # 1) real files this session touched (call arguments + tool results)
    for activity in db.iter_tool_activity(session_id, home):
        tool = activity.get("tool") or ""
        args = activity.get("args") or {}
        created = activity.get("created_at") or 0.0
        source = activity.get("source") or "call"
        candidates: list[str] = []

        result_paths = args.get("__result_paths__") if isinstance(args, dict) else None
        if isinstance(result_paths, list):
            # Tool RESULTS echo back whatever the tool merely READ too, so only a
            # PRODUCER tool's result can create an artifact. Without this gate the
            # pane fills with every repo file a search surfaced.
            if not db._MUTATING_TOOL_RE.search(tool):
                continue
            candidates.extend(result_paths)
        elif isinstance(args, dict):
            # Only a producer tool can create an artifact; matching on argument
            # keys alone wrongly promoted read-only tools (read_file, grep).
            if not db._MUTATING_TOOL_RE.search(tool):
                continue
            for key in db._PATH_ARG_KEYS:
                value = args.get(key)
                if isinstance(value, str) and value.strip():
                    candidates.append(value.strip())
            for key in db._LISTY_ARG_KEYS:
                value = args.get(key)
                if isinstance(value, list):
                    candidates.extend(
                        str(v).strip() for v in value if isinstance(v, str) and v.strip()
                    )

        for raw in candidates:
            key = _dedupe_key(str(raw).strip().strip('"').strip("'"))
            if key in seen:
                continue
            entry = _describe(str(raw), tool, created, source)
            if not entry:
                continue
            seen.add(key)
            artifacts.append(entry)

    # 2) the latest tasklist, as a virtual checklist entry
    try:
        plan = db.latest_tasklist(session_id, home)
    except Exception:
        plan = None
    if plan:
        key = _dedupe_key(plan["path"])
        if key not in seen:
            seen.add(key)
            artifacts.append(plan)

    # 3) workspace scan: files under the session cwd that this session could
    #    plausibly have produced.
    #
    #    It MUST NOT fall back to the Hermes home. A session row with no cwd
    #    (very common — most chats are started without one) previously scanned
    #    all of <home>, which turned a 22-artifact pane into 150 rows of
    #    unrelated repo files: exactly the "it shows everything" complaint. With
    #    no cwd there is nothing session-specific to scan, so it is skipped.
    if scan:
        now = time.time()
        started = None
        cwd = None
        try:
            for row in db.list_sessions(home, limit=200):
                if row.get("session_id") == session_id:
                    started = float(row.get("started_at") or 0.0) or None
                    if row.get("cwd"):
                        cwd = Path(row["cwd"])
                    break
        except Exception:
            cwd = None
        # Only files touched after the session began are this session's work.
        # A small grace period absorbs clock skew between the DB and the FS.
        cutoff = (started - 300.0) if started else (now - 86400.0)
        if cwd is not None and cwd.is_dir():
            try:
                for p in cwd.rglob("*"):
                    if len(artifacts) >= MAX_ARTIFACTS:
                        break
                    try:
                        if not p.is_file():
                            continue
                        mtime = p.stat().st_mtime
                    except Exception:
                        continue
                    if mtime < cutoff:
                        continue
                    key = _dedupe_key(str(p))
                    if key in seen:
                        continue
                    entry = _describe(str(p), "workspace", mtime, "workspace")
                    if not entry:
                        continue
                    seen.add(key)
                    artifacts.append(entry)
            except Exception:
                pass

    artifacts = artifacts[:MAX_ARTIFACTS]
    # Openable first, then existing, then the rest; newest first within a band.
    artifacts.sort(
        key=lambda item: (
            0 if item.get("readable") else (1 if item.get("exists") else 2),
            -float(item.get("created_at") or 0.0),
            str(item.get("label") or ""),
        )
    )

    groups: dict[str, int] = {}
    for item in artifacts:
        name = item.get("group") or "Other"
        groups[name] = groups.get(name, 0) + 1

    # Counted in the loop above, where each rejection reason is actually known.
    # Deriving it as seen-listed would wrongly bill a dedupe hit as a refusal.
    hook_items = hook_listed
    return {
        "session_id": session_id or None,
        "artifacts": artifacts,
        "groups": groups,
        "counts": {"total": len(artifacts), "groups": groups},
        "diagnostics": {
            "home": str(home),
            "db": str(db.db_path(home)),
            "db_exists": db.db_path(home).is_file(),
            "scanned": bool(scan),
            # The live post_tool_call store. `hook_items` is what the hook
            # contributed to this index; `hook_refused` is how many recorded rows
            # were dropped as out-of-home, credential-denied, or noise — non-zero
            # is expected and is the safety gate doing its job, not a fault.
            "store": _store_path(),
            "hook_items": hook_items,
            "hook_refused": hook_refused,
            "generated_at": time.time(),
        },
    }


def _store_path() -> str:
    try:
        return str(_athena_store().state_path())
    except Exception:
        return ""


@router.get("/health")
async def health() -> dict[str, Any]:
    try:
        db = _state_db()
        home = _home()
        return {
            "ok": True,
            "plugin": PLUGIN_ID,
            "db_exists": db.db_path(home).is_file(),
            "home": str(home),
        }
    except Exception as exc:
        return {"ok": False, "plugin": PLUGIN_ID, "error": str(exc)}


@router.get("/index")
async def index(
    session: Optional[str] = Query(None),
    scan: int = Query(0),
    profile: Optional[str] = Query(None),
) -> dict[str, Any]:
    session_id = str(session or "").strip()
    if not session_id:
        return {
            "session_id": None,
            "artifacts": [],
            "groups": {},
            "counts": {"total": 0, "groups": {}},
            "diagnostics": {"error": "no session id"},
        }
    try:
        return build_index(session_id, scan=bool(scan))
    except Exception as exc:  # the pane must never see a 500
        logger.warning("athena: index failed for %s (%s)", session_id, exc)
        return {
            "session_id": session_id,
            "artifacts": [],
            "groups": {},
            "counts": {"total": 0, "groups": {}},
            "diagnostics": {"error": str(exc)},
        }


_TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".json", ".yaml", ".yml", ".toml", ".py",
    ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".css", ".scss", ".html",
    ".htm", ".csv", ".log", ".sh", ".bash", ".bat", ".ps1", ".sql", ".xml",
    ".ini", ".cfg", ".rst", ".adoc",
}


@router.get("/file")
async def file(
    session: Optional[str] = Query(None),
    path: Optional[str] = Query(""),
) -> Any:
    """Serve one artifact's content.

    A ``plan://`` path is virtual: it returns the synthesized tasklist as JSON so
    the pane renders a live checklist instead of reading a nonexistent file.
    """
    from fastapi.responses import JSONResponse, Response

    raw = str(path or "").strip().strip('"').strip("'")
    if not raw:
        return JSONResponse({"error": "missing path"}, status_code=404)

    if raw.startswith("plan://"):
        session_id = str(session or "").strip()
        plan = None
        if session_id:
            try:
                plan = _state_db().latest_tasklist(session_id, _home())
            except Exception:
                plan = None
        if not plan:
            return JSONResponse({"error": "no tasklist for this session"}, status_code=404)
        return JSONResponse(
            {
                "ok": True,
                "virtual": True,
                "kind": "plan",
                "label": plan["label"],
                "task_count": plan["task_count"],
                "task_done": plan["task_done"],
                "tasks": plan["tasks"],
            }
        )

    target = Path(raw)
    try:
        resolved = target.resolve()
    except Exception:
        return JSONResponse({"error": "invalid path"}, status_code=404)
    if not _safe_within_home(resolved):
        return JSONResponse({"error": "path outside hermes home"}, status_code=404)
    try:
        if _is_denied(resolved):
            return JSONResponse({"error": "refused: credential path"}, status_code=404)
    except Exception:
        pass
    if not resolved.is_file():
        return JSONResponse({"error": "file not found"}, status_code=404)
    try:
        size = resolved.stat().st_size
    except Exception:
        return JSONResponse({"error": "file not found"}, status_code=404)
    if size > MAX_FILE_BYTES:
        return JSONResponse({"error": "file too large"}, status_code=404)

    headers = {"X-Athena-Size": str(size)}
    if resolved.suffix.lower() in _TEXT_SUFFIXES:
        # Read through artifact_core.safe_read, NOT raw bytes: it applies the
        # secret redaction pass (an in-home config.yaml would otherwise be
        # served verbatim, leaking API keys the rest of Hermes masks) and caps
        # the decode. The deny-list check above already ran.
        #
        # NOTE the shape: `ctx.rest` is typed `Promise<T>` and always parses the
        # body as JSON, so this route MUST answer with a JSON object. Returning
        # bare text/plain produced "status 200, invalid JSON" in the pane and
        # made every non-plan file unopenable.
        text = ""
        redacted = False
        try:
            safe = _artifact_core().safe_read(str(resolved), max_bytes=MAX_FILE_BYTES)
        except Exception:
            safe = None
        if safe and safe.get("ok"):
            text = str(safe.get("text") or "")
            redacted = bool(safe.get("redacted"))
        else:
            try:
                text = resolved.read_bytes().decode("utf-8-sig", errors="replace")
            except Exception:
                return JSONResponse({"error": "unreadable file"}, status_code=404)
        payload = {
            "ok": True,
            "virtual": False,
            "kind": "text",
            "path": str(resolved),
            "name": resolved.name,
            "ext": resolved.suffix.lower(),
            "size_bytes": size,
            "binary": False,
            "text": text,
            "truncated": bool(safe.get("truncated")) if isinstance(safe, dict) else False,
            "redacted": redacted,
            "language": (safe.get("language") if isinstance(safe, dict) else None),
        }
        return JSONResponse(payload, headers={**headers, "X-Athena-Redacted": "1"} if redacted else headers)
    # Binary: the pane cannot render bytes over a JSON transport, so report the
    # metadata and let the viewer use the preload bridge for images/HTML.
    try:
        size_bytes = resolved.stat().st_size
    except Exception:
        size_bytes = size
    return JSONResponse(
        {
            "ok": True,
            "virtual": False,
            "kind": "binary",
            "path": str(resolved),
            "name": resolved.name,
            "ext": resolved.suffix.lower(),
            "size_bytes": size_bytes,
            "binary": True,
            "text": "",
        },
        headers=headers,
    )


@router.get("/events")
async def events(session: Optional[str] = Query(None)):
    """SSE mirror of ``/index``.

    The desktop pane polls rather than subscribing — the plugin renderer does not
    guarantee the EventSource global. This endpoint exists so the data path can be
    exercised with curl and so a future subscriber has a ready stream.
    """
    from fastapi.responses import StreamingResponse

    session_id = str(session or "").strip()
    if not session_id:
        return StreamingResponse(
            iter(["event: error\ndata: missing session\n\n"]),
            media_type="text/event-stream",
        )

    async def generate():
        import asyncio

        last = None
        try:
            while True:
                try:
                    payload = build_index(session_id)
                    sig = len(payload.get("artifacts") or [])
                except Exception:
                    sig = -1
                if sig != last:
                    last = sig
                    yield "event: index\ndata: " + json.dumps(
                        payload.get("artifacts") or []
                    ) + "\n\n"
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            return
    return StreamingResponse(generate(), media_type="text/event-stream")
    return StreamingResponse(generate(), media_type="text/event-stream")

@router.get("/sessions")
async def list_sessions() -> dict[str, Any]:
    """Every session this instance has seen, with a live artifact count.

    Athena's default shows the FOCUSED session alone. This route is the feed
    for "all sessions" mode: a left-side full tab that lists every session the
    focused profile has touched, newest first, with an artifact count per row.
    Sessions carry their own containment already, so no index guards here.
    """
    try:
        db = _state_db()
        home = _home()
        rows = []
        for session in db.list_sessions(home, limit=500) or []:
            sid = str(session.get("session_id") or "")
            started = str(session.get("started_at") or "")
            title = str(session.get("title") or session.get("display_name") or sid)
            rows.append(
                {
                    "session_id": sid,
                    "title": title,
                    "cwd": str(session.get("cwd") or ""),
                    "started_at": started,
                    "artifact_count": len(
                        build_index(sid).get("artifacts") or []
                    ),
                }
            )
        rows.sort(key=lambda r: r.get("started_at") or "", reverse=True)
        return {"sessions": rows, "count": len(rows)}
    except Exception as exc:
        return {"sessions": [], "count": 0, "error": str(exc)}

