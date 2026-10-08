"""athena — Hermes plugin root entry.

Athena has two halves: a dashboard pane (right-side artifact viewer) and this
process-side half, whose only job is to keep a LIVE index of the files Hermes
produces.

The pane reads its index from two places:

* ``athena_store``'s JSON store, written by the ``post_tool_call`` hook
  registered below — current-session writes, visible immediately.
* ``state_db``'s transcript reader — the backfill for older sessions and for
  anything that predates this hook (or was written where the hook did not fire).

:func:`register` is deliberately import-safe: the Hermes CLI imports this module
during plugin validation and may call ``register()`` with no context at all, so
a missing or partial ``ctx`` must never raise here.
"""

import sys
from pathlib import Path

_here = Path(__file__).parent
for _c in (_here.resolve(), _here):
    if str(_c) not in sys.path:
        sys.path.insert(0, str(_c))

try:
    import athena_hooks
except Exception:  # pragma: no cover - a broken import must not kill validation
    athena_hooks = None


def register(ctx=None):
    """Register Athena's ``post_tool_call`` observer. Safe to call bare."""
    if ctx is None:
        return None
    if athena_hooks is None:
        return None
    register_hook = getattr(ctx, "register_hook", None)
    if not callable(register_hook):
        return None
    try:
        register_hook("post_tool_call", athena_hooks.on_post_tool_call)
    except Exception:
        # A hook-registration failure degrades Athena to transcript-only
        # indexing; it must not stop the rest of the plugin from loading.
        return None
    return None


__all__ = ['register']
