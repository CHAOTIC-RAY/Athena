"""
athena_store.py — live index of paths Hermes produced, as JSON under plugin-data.

WHY THIS EXISTS
---------------
Athena originally had ONE data source: the SQLite session transcript
(``state_db.py``), re-derived on every ``/index`` request. That source is
complete but *reactive* — an artifact only appears once its turn has been
persisted, it is scoped to the one session id the pane was opened with, and it
silently loses anything the transcript never recorded (a write performed by a
subagent whose tool-call row was pruned, a file produced before the current
session, a path that only ever appeared inside a MEDIA marker).

``made-shelf`` already solved the same problem for its own gallery with a
``post_tool_call`` observer hook writing a small JSON store. This module is the
Athena equivalent, reusing that proven shape:

* one bounded JSON file (``<hermes_home>/plugin-data/athena/index.json``),
* atomic writes (tmp file + :func:`os.replace`) so a crash mid-save cannot leave
  a half-written index,
* case-insensitive dedupe by path — re-recording bumps an entry to newest while
  preserving its ``pinned`` flag,
* a ``MAX_ITEMS`` cap that drops the OLDEST UNPINNED entries first.

It is strictly READ-ONLY with respect to user files: it records path strings
only. Nothing here opens, writes, moves or deletes anything the user owns.

Every public function is total — bad input degrades to ``None`` / an empty
snapshot rather than raising — because the hook runs inside the agent's tool
loop and an exception here would be attributed to the user's own tool call.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

PLUGIN_ID = "athena"
STATE_FILENAME = "index.json"
MAX_ITEMS = 200

# Serialise the read-modify-write cycle. post_tool_call fires on a worker
# thread and parallel tool calls really do overlap; without this, two writers
# both read the same list and the second os.replace silently discards the
# first writer's entry.
_WRITE_LOCK = threading.Lock()

# Ceilings for the text we copy out of a tool call/result. Long summaries are
# display-only, so truncating is cheaper and safer than storing the blob.
MAX_PATH = 500
MAX_NAME = 214
MAX_TOOL = 64
MAX_SESSION = 200
MAX_SUMMARY = 240


# ── location ────────────────────────────────────────────────────────────────
def data_dir() -> Path:
    """The plugin-data directory for athena.

    Prefers Hermes' own ``plugin_data_dir`` helper (it owns profile scoping and
    directory creation) and falls back to ``<HERMES_HOME>/plugin-data/athena``
    when the helper is not importable — e.g. while ``selftest.py`` exercises
    this module standalone, or under a profile whose plugin tree is incomplete.
    """
    try:
        from plugins.plugin_storage import plugin_data_dir  # type: ignore

        return plugin_data_dir(PLUGIN_ID)
    except Exception:
        home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
        path = home / "plugin-data" / PLUGIN_ID
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return path


def state_path() -> Path:
    """Absolute path of the JSON store."""
    try:
        return data_dir() / STATE_FILENAME
    except Exception:
        return Path.home() / ".hermes" / "plugin-data" / PLUGIN_ID / STATE_FILENAME


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def epoch_of(iso: str) -> float:
    """Parse a stored ``at`` stamp back to epoch seconds; 0.0 when unusable.

    ``/index`` sorts by recency, so an unparseable stamp must degrade to "oldest"
    rather than raise inside a request handler.
    """
    try:
        text = str(iso or "").strip()
        if not text:
            return 0.0
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


# ── state io ────────────────────────────────────────────────────────────────
def _empty() -> dict[str, Any]:
    return {"items": []}


def load_state() -> dict[str, Any]:
    """Read the store, tolerating absence and corruption. Never raises."""
    try:
        path = state_path()
    except Exception:
        return _empty()
    try:
        if not path.is_file():
            return _empty()
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        # A truncated/corrupt store must not take the dashboard down: start clean.
        return _empty()
    if not isinstance(raw, dict):
        return _empty()
    if not isinstance(raw.get("items"), list):
        raw["items"] = []
    return raw


def save_state(state: dict[str, Any]) -> bool:
    """Atomically persist the store. Returns True on success; never raises."""
    try:
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        os.replace(tmp, path)
        return True
    except Exception:
        return False


# ── path hygiene ────────────────────────────────────────────────────────────
def _normalize_path(raw: object) -> Optional[str]:
    """Clean one candidate path, or ``None`` when it is not a usable path.

    The store is a path INDEX, not a content store, so anything that is not a
    filesystem path is dropped here rather than downstream: a URL, an over-long
    value, a plan:// pseudo-address or a non-string.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip().strip('"').strip("'")
    if not text or len(text) > MAX_PATH:
        return None
    if "\n" in text or "\x00" in text:
        return None
    # Virtual plan:// addresses belong to the transcript reader, not here.
    if text.startswith("plan://"):
        return None
    # A URI is not a path. `file:` is allowed through: it addresses a real file.
    if "://" in text and not text.lower().startswith("file:"):
        return None
    return text


def extract_paths(tool_name: str, args: dict | None) -> list[str]:
    """Paths named by a tool's arguments, deduped case-insensitively, in order."""
    payload = args if isinstance(args, dict) else {}
    found: list[str] = []
    for key in ("path", "file", "file_path", "filepath", "filename", "target",
                "output_path", "out", "resolved_path", "destination"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            cleaned = _normalize_path(value)
            if cleaned:
                found.append(cleaned)
        elif isinstance(value, list):
            # Some tools batch under a single key.
            for item in value:
                cleaned = _normalize_path(item)
                if cleaned:
                    found.append(cleaned)
    for key in ("paths", "files", "file_paths", "targets", "file_list"):
        value = payload.get(key)
        if isinstance(value, list):
            for item in value:
                cleaned = _normalize_path(item)
                if cleaned:
                    found.append(cleaned)

    out: list[str] = []
    seen: set[str] = set()
    for item in found:
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    del tool_name
    return out


def _key(path: str) -> str:
    """Case-insensitive dedupe key (also separator-normalised on Windows)."""
    try:
        return os.path.normcase(os.path.normpath(str(path))).casefold()
    except Exception:
        return str(path).casefold()


# ── writes ──────────────────────────────────────────────────────────────────
def record_made(
    *,
    path: str,
    tool_name: str,
    session_id: str = "",
    summary: str = "",
) -> dict[str, Any] | None:
    """Record one produced path. Returns the stored entry, or ``None`` if unusable.

    Re-recording an existing path is an UPSERT: the entry moves to newest and
    keeps its ``pinned`` flag, so pinning survives repeated writes to one file.
    """
    try:
        clean = _normalize_path(path)
        if not clean:
            return None
        name = Path(clean).name or clean
        entry: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "at": now_iso(),
            "tool": str(tool_name or "")[:MAX_TOOL],
            "path": clean[:MAX_PATH],
            "name": name[:MAX_NAME],
            "session_id": str(session_id or "")[:MAX_SESSION],
            "summary": (summary or "")[:MAX_SUMMARY],
            "pinned": False,
        }
        target = _key(clean)
        with _WRITE_LOCK:
            # The whole read-modify-write cycle is inside the lock: parallel tool
            # calls really do fire post_tool_call on separate threads, and a load
            # taken before acquiring it would be stale by the time we save.
            state = load_state()
            items: list[dict[str, Any]] = [
                it for it in state.get("items", []) if isinstance(it, dict)
            ]
            pinned = False
            rest: list[dict[str, Any]] = []
            for it in items:
                if _key(it.get("path") or "") == target:
                    pinned = bool(it.get("pinned"))
                    continue
                rest.append(it)
            entry["pinned"] = pinned
            rest.append(entry)

            if len(rest) > MAX_ITEMS:
                # Pinned entries are kept even when that overflows the cap; the
                # oldest UNPINNED entries go first, hence the `at` sort.
                keep = sorted(
                    (it for it in rest if it.get("pinned")),
                    key=lambda it: str(it.get("at") or ""),
                )
                room = MAX_ITEMS - len(keep)
                plain = sorted(
                    (it for it in rest if not it.get("pinned")),
                    key=lambda it: str(it.get("at") or ""),
                )
                plain = plain[-room:] if room > 0 else []
                rest = keep + plain

            state["items"] = rest
            save_state(state)
        return entry
    except Exception:
        return None


def set_pinned(item_id: str, pinned: bool) -> bool:
    """Pin/unpin an entry by id. Returns False when the id is unknown."""
    try:
        state = load_state()
        items = [it for it in state.get("items", []) if isinstance(it, dict)]
        found = False
        for it in items:
            if str(it.get("id") or "") == str(item_id):
                it["pinned"] = bool(pinned)
                found = True
                break
        if not found:
            return False
        state["items"] = items
        return save_state(state)
    except Exception:
        return False


def clear_items(*, keep_pinned: bool = True) -> int:
    """Drop entries, keeping pinned ones by default. Returns how many were removed."""
    try:
        state = load_state()
        items = [it for it in state.get("items", []) if isinstance(it, dict)]
        if keep_pinned:
            kept = [it for it in items if it.get("pinned")]
            removed = len(items) - len(kept)
            state["items"] = kept
        else:
            removed = len(items)
            state["items"] = []
        save_state(state)
        return removed
    except Exception:
        return 0


# ── reads ───────────────────────────────────────────────────────────────────
def list_items(*, pinned_only: bool = False) -> list[dict[str, Any]]:
    """Recorded entries, oldest first, matching the store's on-disk order."""
    try:
        items = [
            it
            for it in load_state().get("items", [])
            if isinstance(it, dict) and it.get("path")
        ]
        if pinned_only:
            items = [it for it in items if it.get("pinned")]
        return items
    except Exception:
        return []


def snapshot() -> dict[str, Any]:
    """The store's current contents, newest first. Never raises."""
    try:
        items = list_items()
        ordered = sorted(items, key=lambda it: epoch_of(it.get("at") or ""))
        return {
            "ok": True,
            "count": len(ordered),
            "pinned_count": sum(1 for it in ordered if it.get("pinned")),
            "items": ordered,
            "path": str(state_path()),
        }
    except Exception:
        return {"ok": False, "count": 0, "pinned_count": 0, "items": [], "path": ""}


__all__ = [
    "PLUGIN_ID",
    "MAX_ITEMS",
    "STATE_FILENAME",
    "data_dir",
    "state_path",
    "now_iso",
    "epoch_of",
    "load_state",
    "save_state",
    "extract_paths",
    "record_made",
    "set_pinned",
    "clear_items",
    "list_items",
    "snapshot",
]