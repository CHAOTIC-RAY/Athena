"""
athena_hooks.py — the live half of Athena's index.

``register(ctx)`` wires :func:`on_post_tool_call` into Hermes' ``post_tool_call``
observer hook. Every time the agent finishes a tool call, this runs and records
the paths that call PRODUCED, so ``/index`` reflects a file the moment it is
written instead of waiting for the transcript row to be read back.

Two sources of paths, deliberately kept narrow:

``write_file`` / ``patch``
    Their ARGUMENTS name the file being written. That is the precise,
    structured signal — no text scraping involved.

``execute_code`` / ``terminal``
    These write files but name no output path, so their ARGUMENTS are useless as
    an index. Their RESULT text is scanned for ``MEDIA:`` markers only, which
    is how Hermes delivers an image/audio/video file it just produced. The
    general "absolute path in result text" heuristic is NOT used: a shell
    command that merely READS a repo file would fill the panel with the repo.

Read-only tools (``read_file``, ``grep``, ``search_files``, …) are never watched.

The hook is an OBSERVER: it runs inside the agent's tool loop, so it must never
raise and never write user content. Every body here is wrapped, and the only
side effect is appending a path string to the JSON store.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# Tools whose arguments name the file they wrote.
WATCH = {"write_file", "patch"}

# Tools that produce files without naming them, so their ARGUMENTS are useless as
# an index. Only MEDIA markers in the RESULT count for these — that is how Hermes
# hands a file back to the client. execute_code/terminal emit them for images
# they wrote; browser_vision (screenshot_path) and text_to_speech (audio file)
# emit them for media they captured. The general "absolute path in result text"
# heuristic is deliberately NOT used: a shell command that merely READS a repo
# file would fill the panel with the whole repo.
MEDIA_WATCH = {
    "execute_code",
    "terminal",
    "browser_vision",
    "text_to_speech",
}

# Matches the marker Hermes puts in tool output to hand a media file to the
# client, e.g. ``MEDIA: D:\\...\\shot.png``. Stop at the line end.
_MEDIA_MARKER_RE = re.compile(r"MEDIA:\s*([^\r\n]+)")

# An explicit failure means the file was probably NOT written.
_FAIL_STATUSES = {"error", "failed", "failure", "blocked", "denied"}

# Toolchain/runtime noise, applied to MEDIA paths ONLY.
#
# This list is deliberately narrow — node_modules, site-packages, __pycache__,
# VCS internals, package-manager caches. It does NOT include Hermes' own scratch
# locations (AppData\Local\Temp, audio_cache, screenshot dirs): a MEDIA marker is
# a capture tool stating "here is a file I made for the user", and on a real
# install those directories ARE where the file lands. Filtering them by name
# would veto exactly the screenshots and audio Athena exists to show. The real
# boundary is in /index, where every recorded path must resolve inside the Hermes
# home and must clear artifact_core.is_denied.
_NOISE_RE = re.compile(
    r"(node_modules|site-packages|__pycache__|[\\/]\.git[\\/]|"
    r"\\uv\\cache|\\pip\\cache|hermes_kernel_)",
    re.IGNORECASE,
)


def _store() -> Any:
    """Resolve athena_store.

    Imported by name when the plugin dir is on ``sys.path`` (which
    ``__init__.register`` guarantees), otherwise loaded from this file's own
    directory so the hook also works when imported directly by the self-test.
    """
    try:
        import athena_store  # type: ignore

        return athena_store
    except Exception:
        import importlib.util
        import sys

        name = "athena_store"
        if name in sys.modules:
            return sys.modules[name]
        path = Path(__file__).resolve().parent / "athena_store.py"
        spec = importlib.util.spec_from_file_location(name, str(path))
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module


def _clean_result_text(result: object) -> str:
    """Flatten a tool result to text, whether it arrived as str or dict."""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("output", "text", "stdout", "result", "content"):
            value = result.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _media_paths(result: object) -> list[str]:
    """Paths from ``MEDIA:`` markers in a result, deduped, noise-filtered."""
    out: list[str] = []
    seen: set[str] = set()
    for marker in _MEDIA_MARKER_RE.findall(_clean_result_text(result)):
        candidate = marker.strip().strip('"').strip("'")
        candidate = re.sub(r"[:(]\s*$", "", candidate).strip()
        if not candidate or _NOISE_RE.search(candidate):
            continue
        key = candidate.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def on_post_tool_call(
    tool_name: str = "",
    args: dict | None = None,
    result: str = "",
    session_id: str = "",
    status: str = "",
    **kwargs,
) -> None:
    """``post_tool_call`` observer. Records produced paths; never raises.

    Returns ``None`` always — the hook's return value is ignored by Hermes, and
    raising here would be reported against the user's tool call.
    """
    del kwargs
    try:
        name = str(tool_name or "").strip()
        if name not in WATCH and name not in MEDIA_WATCH:
            return
        if str(status or "").strip().lower() in _FAIL_STATUSES:
            return

        store = _store()
        if name in WATCH:
            paths = store.extract_paths(name, args if isinstance(args, dict) else {})
        else:
            paths = _media_paths(result)
        if not paths:
            return

        summary = _clean_result_text(result).strip().replace("\n", " ")[:240]
        for path in paths:
            store.record_made(
                path=path,
                tool_name=name,
                session_id=str(session_id or ""),
                summary=summary,
            )
    except Exception:
        # An observer must be invisible: a bug here cannot be allowed to surface
        # as a failed write_file.
        return


__all__ = ["WATCH", "MEDIA_WATCH", "on_post_tool_call"]