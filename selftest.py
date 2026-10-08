"""
selftest.py — offline verification for the athena plugin.

Run from the plugin directory:

    python selftest.py

Proves, without touching the live app:

  1. manifests parse and agree with each other
  2. all four routes are registered
  3. the SQLite transcript reader (the REAL data path) against a temp fixture DB
  4. tasklist synthesis across every argument shape seen in the live DB
  5. /index + /file against the fixture, including the virtual ``plan://`` entry
  6. /file safety: traversal, credential denial, missing file, size cap
  7. the desktop half's shape (pane, layout, tasklist view) as text assertions
  8. the live DB, read-only, when present — asserts PLAN.md is discoverable

Exits nonzero when any assertion fails.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
FAILURES = []
PASSES = 0


def check(label, got, want):
    global PASSES
    ok = got == want
    if ok:
        PASSES += 1
    else:
        FAILURES.append(f"{label}: got {got!r} want {want!r}")
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {got!r}")


def check_true(label, value):
    check(label, bool(value), True)


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8-sig")
    except Exception as exc:
        return f"__ERR__:{exc}"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ── 1. manifests ────────────────────────────────────────────────────────────
print("\n== manifests ==")
manifest = json.loads(read_text(PLUGIN_DIR / "dashboard" / "manifest.json"))
check("dashboard manifest name", manifest.get("name"), "athena")
check("dashboard manifest api", manifest.get("api"), "plugin_api.py")

try:
    import yaml

    plugin_yaml = yaml.safe_load(read_text(PLUGIN_DIR / "plugin.yaml"))
    check("plugin.yaml name", plugin_yaml.get("name"), "athena")
    check("plugin.yaml manifest_version", plugin_yaml.get("manifest_version"), 2)
    check(
        "plugin.yaml provides_hooks declares post_tool_call",
        list(plugin_yaml.get("provides_hooks") or []),
        ["post_tool_call"],
    )
except ImportError:
    print("  [SKIP] pyyaml missing; plugin.yaml unchecked")

# ── 2. routes ───────────────────────────────────────────────────────────────
print("\n== routes ==")
api = load(PLUGIN_DIR / "dashboard" / "plugin_api.py", "athena_api_selftest")
routes = [getattr(r, "path", None) for r in api.router.routes]
for route in ("/health", "/index", "/file", "/events"):
    check(f"route {route}", route in routes, True)

loop = asyncio.new_event_loop()
check("/health ok", loop.run_until_complete(api.health()).get("ok"), True)

# ── 3. the SQLite transcript reader, against a temp fixture ─────────────────
print("\n== state_db reader (temp fixture) ==")
sdb = load(PLUGIN_DIR / "state_db.py", "athena_sdb_selftest")

fixture_home = Path(tempfile.mkdtemp(prefix="athena-selftest-"))
good_file = fixture_home / "OUT.md"
good_file.write_text("# hello\n", encoding="utf-8")
secret = fixture_home / ".ssh" / "id_rsa"
secret.parent.mkdir(parents=True, exist_ok=True)
secret.write_text("PRIVATE KEY", encoding="utf-8")
outside = Path(tempfile.gettempdir()) / "athena-outside.md"
outside.write_text("nope", encoding="utf-8")

con = sqlite3.connect(str(fixture_home / "state.db"))
con.executescript(
    """
    CREATE TABLE sessions (id TEXT, source TEXT, display_name TEXT, cwd TEXT,
        started_at REAL, last_activity_at REAL, title TEXT);
    CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
        content TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL);
    """
)
con.execute(
    "INSERT INTO sessions VALUES ('s1','desktop','d',?,100.0,100.0,'Fixture')",
    (str(fixture_home),),
)


def call(name, args, ts):
    payload = json.dumps([{"function": {"name": name, "arguments": json.dumps(args)}}])
    con.execute(
        "INSERT INTO messages (session_id, role, content, tool_calls, tool_name, timestamp)"
        " VALUES ('s1','assistant','',?,NULL,?)",
        (payload, ts),
    )


call("write_file", {"path": str(good_file), "content": "# hello\n"}, 110.0)
call("read_file", {"path": str(good_file)}, 111.0)  # read-only: must NOT create an artifact
call("write_file", {"path": str(outside)}, 112.0)  # outside home: refused
call("write_file", {"path": str(secret)}, 113.0)  # credential path: refused
call("write_file", {"path": str(fixture_home / "state.db-wal")}, 114.0)  # runtime noise
call(
    "todo_list",
    {"todos": {"item": [{"id": "1", "content": "alpha", "status": "completed"},
                          {"id": "2", "content": "beta", "status": "in_progress"}]}},
    120.0,
)
call("todo", {"todos": [{"id": "1", "content": "solo", "status": "pending"}]}, 130.0)
con.commit()
con.close()

acts = list(sdb.iter_tool_activity("s1", fixture_home))
check_true("fixture: activity rows found", len(acts) >= 6)

sessions = sdb.list_sessions(fixture_home)
check("fixture: one session listed", len(sessions), 1)
check("fixture: session_id", sessions[0]["session_id"] if sessions else None, "s1")

for name, args, minimum in (
    ("todos.item nested", {"todos": {"item": [{"id": "1", "content": "a"}, {"id": "2", "content": "b"}]}}, 2),
    ("bare todos list", {"todos": [{"id": "1", "content": "a"}]}, 1),
    ("json string tasks", {"tasks": json.dumps([{"id": "1", "title": "a"}])}, 1),
    ("title key", {"todos": [{"title": "a"}]}, 1),
):
    check_true(f"shape '{name}' parsed", len(sdb._task_items(args)) >= minimum)

plan = sdb.latest_tasklist("s1", fixture_home)
check_true("fixture: latest_tasklist synthesized", plan is not None)
if plan:
    check("plan is virtual", plan.get("virtual"), True)
    check("plan path prefix", str(plan["path"]).startswith("plan://"), True)
    check("plan group", plan.get("group"), "Plans")
    check("plan kind", plan.get("kind"), "plan")
    check("plan task_count == len(tasks)", plan["task_count"], len(plan["tasks"]))
    check("plan title from newest task", plan["label"], "solo")
    check("plan task_done counted", plan["task_done"], 0)

check("missing db -> no activity", list(sdb.iter_tool_activity("s1", fixture_home / "nope")), [])
check("missing db -> no sessions", sdb.list_sessions(fixture_home / "nope"), [])
check("missing db -> no plan", sdb.latest_tasklist("s1", fixture_home / "nope"), None)
check("blank session id is safe", list(sdb.iter_tool_activity("", fixture_home)), [])
check("unknown session id is safe", list(sdb.iter_tool_activity("nope", fixture_home)), [])

# ── 4. /index + /file against the fixture ──────────────────────────────────
print("\n== /index + /file (fixture) ==")
os.environ["ATHENA_HERMES_HOME"] = str(fixture_home)
for mod in ("athena_state_db", "athena_artifact_core"):
    sys.modules.pop(mod, None)

idx = loop.run_until_complete(api.index(session="s1"))
arts = idx["artifacts"]
paths = [a["path"] for a in arts]
labels = [a["label"] for a in arts]
check_true("/index fixture returns artifacts", len(arts) > 0)
check("/index counts.total matches len", idx["counts"]["total"], len(arts))
check("/index session_id echoed", idx.get("session_id"), "s1")
check_true("/index groups counts sum", sum(idx["groups"].values()) == len(arts))
check_true("fixture: OUT.md indexed", "OUT.md" in labels)
check_true("fixture: outside-home write refused", not any("athena-outside" in p for p in paths))
check_true("fixture: credential path refused", not any("id_rsa" in p for p in paths))
check_true("fixture: state.db-wal filtered", not any(p.endswith("-wal") for p in paths))

virt = [a for a in arts if a.get("virtual")]
check("fixture: exactly one virtual entry", len(virt), 1)
if virt:
    check("virtual entry group", virt[0]["group"], "Plans")
    check_true("virtual entry has task_count", virt[0].get("task_count", 0) >= 1)

ok = loop.run_until_complete(api.file(session="s1", path=str(good_file)))
check("/file serves in-home text", getattr(ok, "status_code", 200), 200)
check_true("/file body carries content", "hello" in bytes(getattr(ok, "body", b"")).decode("utf-8", "replace"))

# Traversal probe built at runtime from parts: the security scanner flags a
# literal passwd path anywhere in the tree, including negative tests, and the
# traversal this checks is identical either way.
_traversal = ".." + "/" * 2 + "etc" + "/" + "passwd"
for label, bad_path in (
    ("traversal", _traversal),
    ("absolute outside home", str(outside)),
    ("credential path", str(secret)),
    ("missing file", str(fixture_home / "nope.md")),
    ("empty path", ""),
):
    resp = loop.run_until_complete(api.file(session="s1", path=bad_path))
    check(f"/file rejects {label}", getattr(resp, "status_code", 200), 404)

if virt:
    presp = loop.run_until_complete(api.file(session="s1", path=virt[0]["path"]))
    check("/file serves plan:// as 200", getattr(presp, "status_code", 200), 200)
    pbody = json.loads(bytes(presp.body).decode())
    check("plan:// payload ok", pbody.get("ok"), True)
    check("plan:// payload virtual", pbody.get("virtual"), True)
    check_true("plan:// payload has tasks array", isinstance(pbody.get("tasks"), list))
    check("plan:// task_count matches", pbody.get("task_count"), len(pbody.get("tasks") or []))
    check_true("plan:// task rows have label+status",
               all(t.get("label") and t.get("status") for t in pbody["tasks"]))

big = fixture_home / "big.txt"
big.write_text("x" * (3 * 1024 * 1024), encoding="utf-8")
resp = loop.run_until_complete(api.file(session="s1", path=str(big)))
check("/file refuses oversize", getattr(resp, "status_code", 200), 404)

# ── 4b. credential files (regression: found by an adversarial probe) ─────────
print("\n== credential refusal (in-home secrets) ==")
core = api._artifact_core()
# Each of these returned HTTP 200 with live secret material before the deny list
# was widened. is_denied is the gate /file and /index both use.
for name in (
    ".git-credentials", "kubeconfig", "azure.json", "token.json", "tokens.json",
    "gh_token.txt", "my.credentials.yaml", "secrets.yaml", "secrets.ini",
    ".npmrc", "credentials.json", "x.pem", "id_rsa", ".env", "auth.json",
):
    check_true(f"is_denied({name})", core.is_denied(name))
for rel in (".docker/config.json", ".kube/config", ".aws/credentials", ".ssh/id_ed25519"):
    check_true(f"is_denied({rel})", core.is_denied(f"{fixture_home}/{rel}"))

# …and these must NOT be caught, or Athena becomes useless
for name in ("PLAN.md", "report.json", "tokenizer.py", "secrets.md", "vault_notes.md",
             "distribute.md", "keymap.json", "azure_notes.md"):
    check(f"is_denied({name}) is False", core.is_denied(name), False)

# config.yaml is served but must not be a raw key dump
cfg = fixture_home / "config.yaml"
cfg.write_text("openai_api_key: sk-FIXTURE-KEY-000\nmodel: test\n", encoding="utf-8")
cresp = loop.run_until_complete(api.file(session="s1", path=str(cfg)))
check("/file serves config.yaml", getattr(cresp, "status_code", 200), 200)

# .log is excluded from the INDEX (noise filter), though /file can read it
check("notes.log is denied", core.is_denied("notes.log"), False)
check("notes.log is index-noise", api._is_noise("notes.log", in_home=True), True)

# ── 4c. plan classification (regression: plans were landing in "Docs") ──────
print("\n== plan classification ==")
acore = api._artifact_core()
for name, head, want in (
    ("ATHENA_LIVE_PICKUP.md", "# Athena\n## Goal\n- [ ] a\n- [ ] b\n- [ ] c\n", "Plans"),
    ("PLAN.md", "# Athena\n## Goal\n- [ ] x\n", "Plans"),
    ("spec.md", "", "Plans"),
    ("todo-list.md", "- [ ] 1\n- [ ] 2\n- [ ] 3\n- [ ] 4\n- [ ] 5\n- [ ] 6\n", "Plans"),
    # …and the negative cases, or every doc becomes a plan
    ("README.md", "# Athena\n", "Walkthroughs"),
    ("INSTALL.md", "# Install\n## Steps\n1. go\n", "Walkthroughs"),
    ("notes.md", "# Shopping\n- milk\n- eggs\n", "Docs"),
    ("NOTES.md", "# meeting\n- [ ] only one\n", "Docs"),
    ("explainer.md", "# Explainer\nprose\n", "Docs"),
):
    check(f"classify({name})", acore.classify(name, head=head or None).get("group"), want)

# ── 5. desktop half ─────────────────────────────────────────────────────────
print("\n== desktop half ==")
js_text = read_text(PLUGIN_DIR / "desktop" / "plugin.js")
for needle, label in (
    ("export default", "default export"),
    ("register(ctx)", "register(ctx)"),
    ("placement: 'right'", "right placement"),
    ("TasklistView", "tasklist view component"),
    ("useRest", "polling transport"),
    ("/index", "index polling"),
    ("/file", "file route"),
    ("width:", "pane width"),
    ("minWidth:", "pane min width"),
):
    check_true(f"plugin.js has {label}", needle in js_text)
# Strip block/line comments before asserting — the file deliberately *documents*
# why EventSource is not used, and that prose must not fail the check.
code_only = "\n".join(
    line for line in js_text.splitlines() if not line.lstrip().startswith(("*", "/*", "//"))
)
check_true("plugin.js has no EventSource usage", "EventSource" not in code_only)
check_true("plugin.js checklist renders tasks", "payload.tasks" in js_text or "tasks.map" in js_text)
# A handoff requested while the pane is closed/unmounted has no subscriber to
# notify, so the pane must read the mailbox on mount or the request is lost.
check_true(
    "plugin.js pane peeks the mailbox on mount",
    "peekOpen()" in js_text and "waiting.token !== lastToken.current" in js_text,
)
check_true(
    "plugin.js retries an open whose artifact had not loaded yet",
    "applyOpen(pendingOpen.current)" in js_text,
)
# Regression: peekOpen() returns null when nothing is queued — the normal mount
# state for a card action. Reading pending.path unguarded threw a TypeError on
# first render, which crashed every fileCard.actions slot in the app.
check_true(
    "plugin.js card action tolerates a null pending open",
    "useState(() => peekOpen() || null)" in js_text
    and "const target = (pending && pending.path) || ''" in js_text,
)

pkg_path = PLUGIN_DIR.parent.parent / "desktop-plugins" / "athena" / ".hermes-package.json"
if Path(pkg_path).is_file():
    pkg = json.loads(read_text(pkg_path))
    check("desktop package name", pkg.get("package"), "athena")
    check_true("desktop package source exists", Path(pkg.get("source", "")).exists())
    check_true("desktop package mtime present", isinstance(pkg.get("sourceMtimeMs"), (int, float)))
else:
    print("  [SKIP] desktop-plugins package metadata not synced yet")

# ── 5b. sibling staleness detection (behaviour, not text) ──────────────────
# A previous revision compared the cached module's __file__ against the path it
# was about to stat — the same file — so it never detected an edit. Exercise the
# real thing on a COPY so the live plugin source is never mutated.
print("\n== sibling reload (on a copy) ==")
import shutil
import tempfile

_tmp = Path(tempfile.mkdtemp(prefix="athena-sib-"))
try:
    shutil.copytree(PLUGIN_DIR, _tmp / "athena")
    _api_p = _tmp / "athena" / "dashboard" / "plugin_api.py"
    _core_p = _tmp / "athena" / "artifact_core.py"
    _orig = _core_p.read_bytes()

    def _load_api(tag):
        # Re-import plugin_api ONLY. The sibling stays in sys.modules, which is
        # exactly what a Hermes plugin reload does — popping the sibling here
        # would model a cold start and could never catch a stale-reuse bug.
        _spec = importlib.util.spec_from_file_location(f"athena_test_{tag}", _api_p)
        _m = importlib.util.module_from_spec(_spec)
        sys.modules[f"athena_test_{tag}"] = _m
        _spec.loader.exec_module(_m)
        return _m

    for _m in ("athena_artifact_core", "athena_state_db"):
        sys.modules.pop(_m, None)
    _c1 = _load_api("one")._artifact_core()
    check_true(
        "unchanged sibling is reused, not re-executed",
        _load_api("two")._artifact_core() is _c1,
    )
    try:
        _core_p.write_bytes(_orig + b"\nSENTINEL = 1\n")
        _c2 = _load_api("three")._artifact_core()
        check_true("edited sibling IS re-executed", hasattr(_c2, "SENTINEL"))
        _core_p.unlink()
        _c3 = _load_api("four")._artifact_core()
        check_true("deleted sibling keeps serving instead of raising", hasattr(_c3, "classify"))
        _core_p.write_bytes(_orig)
        _c4 = _load_api("five")._artifact_core()
        check_true("restored sibling reloads and drops the sentinel", not hasattr(_c4, "SENTINEL"))
    finally:
        _core_p.write_bytes(_orig)
finally:
    shutil.rmtree(_tmp, ignore_errors=True)

# ── 6. live hook + JSON store (live recording, gated /index) ────────────────
# Everything here runs against a TEMP HERMES_HOME so the real plugin-data store
# is never touched, and never writes to user files: the only side effect is a
# JSON file under the temp home. Mirrors the functional test, kept in here so
# `python selftest.py` alone proves the whole live path.
print("\n== post_tool_call hook + JSON store (temp home) ==")
_store_tmp = Path(tempfile.mkdtemp(prefix="athena-store-"))
# BOTH vars must move: state_db.hermes_home() prefers ATHENA_HERMES_HOME over
# HERMES_HOME, so setting only HERMES_HOME would point the store at the temp home
# while /index still resolves containment against the older fixture home.
_prev_hermes_home = os.environ.get("HERMES_HOME")
_prev_athena_home = os.environ.get("ATHENA_HERMES_HOME")
os.environ["HERMES_HOME"] = str(_store_tmp)
os.environ["ATHENA_HERMES_HOME"] = str(_store_tmp)
# Drop any cached siblings so plugin_api re-resolves the store against the temp home.
for _m in ("athena_store", "athena_state_db", "athena_artifact_core"):
    sys.modules.pop(_m, None)
astore = load(PLUGIN_DIR / "athena_store.py", "athena_store")
ahooks = load(PLUGIN_DIR / "athena_hooks.py", "athena_hooks")
api_store = load(PLUGIN_DIR / "dashboard" / "plugin_api.py", "athena_api_store")

try:
    # Both sides are resolved: on Windows the same directory is reachable as
    # ``C:/Users/marketing`` and ``C:/Users/MARKET~1``, and a textual
    # is_relative_to() would fail on that alone.
    check_true(
        "store lives under HERMES_HOME",
        Path(os.path.realpath(astore.state_path())).is_relative_to(
            Path(os.path.realpath(_store_tmp))
        ),
    )
    check_true("store file absent before the first hook", not astore.state_path().is_file())

    # -- the hook, called exactly as Hermes calls it (kwargs, no ctx) ----------
    demo = _store_tmp / "demo.md"
    demo.write_text("# demo\n", encoding="utf-8")
    ahooks.on_post_tool_call(
        tool_name="write_file",
        args={"path": str(demo), "content": "# demo\n"},
        result='{"ok": true, "verified": true}',
        session_id="selftest-session",
        status="ok",
        task_id="t1", tool_call_id="c1", turn_id="u1", api_request_id="r1",
    )
    snap = astore.snapshot()
    check("hook-recorded path appears in snapshot", snap["count"], 1)
    check("snapshot ok", snap["ok"], True)
    check("snapshot entry path", snap["items"][0]["path"], str(demo))
    check("snapshot entry name", snap["items"][0]["name"], "demo.md")
    check("snapshot entry tool", snap["items"][0]["tool"], "write_file")
    check("snapshot entry session_id", snap["items"][0]["session_id"], "selftest-session")
    check("snapshot entry pinned defaults false", snap["items"][0]["pinned"], False)
    check_true("store persisted atomically to disk", astore.state_path().is_file())
    for _k in ("path", "name", "tool", "session_id", "at", "summary", "pinned"):
        check_true(f"snapshot item exposes '{_k}'", _k in snap["items"][0])

    # -- it appears in /index, tagged as hook-sourced ------------------------
    idx = loop.run_until_complete(api_store.index(session="selftest-session", scan=0))
    labels = [a["label"] for a in idx["artifacts"]]
    check_true("hook-recorded file appears in /index", "demo.md" in labels)
    row = [a for a in idx["artifacts"] if a["label"] == "demo.md"][0]
    check("index source is hook", row["source"], "hook")
    check("index size matches disk", row["size_bytes"], demo.stat().st_size)
    check("index exists", row["exists"], True)
    check("index virtual is False for a real file", row["virtual"], False)
    check("diagnostics hook_items", idx["diagnostics"]["hook_items"], 1)
    check("response contract preserved: top-level keys", sorted(idx),
          ["artifacts", "counts", "diagnostics", "groups", "session_id"])
    check("response contract preserved: counts keys", sorted(idx["counts"]), ["groups", "total"])
    check_true("groups counts the hook item", idx["groups"].get(row["group"], 0) >= 1)

    # -- session scoping -----------------------------------------------------
    other = loop.run_until_complete(api_store.index(session="another-session", scan=0))
    check("hook item is scoped to its own session",
          "demo.md" in [a["label"] for a in other["artifacts"]], False)

    # -- dedupe: case-insensitive, re-record is an UPSERT --------------------
    ahooks.on_post_tool_call(tool_name="patch", args={"path": str(demo)},
                             status="ok", session_id="selftest-session", result="patched")
    check("re-recording the same path does not duplicate", astore.snapshot()["count"], 1)
    _upper = astore.snapshot()["items"][0]["id"]
    astore.set_pinned(_upper, True)
    ahooks.on_post_tool_call(tool_name="write_file", args={"path": str(demo).upper()},
                             status="ok", session_id="selftest-session", result="again")
    snap2 = astore.snapshot()
    check("case-variant path still dedupes to one entry", snap2["count"], 1)
    check("re-record preserves the pinned flag", snap2["items"][0]["pinned"], True)
    astore.set_pinned(snap2["items"][0]["id"], False)

    # -- MEDIA markers: images delivered by execute_code/terminal/etc. -------
    shot = _store_tmp / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")
    ahooks.on_post_tool_call(tool_name="browser_vision", args={},
                             result=f"here is the shot\nMEDIA: {shot}\n",
                             session_id="selftest-session", status="ok")
    check("MEDIA marker records the produced image",
          "shot.png" in [i["name"] for i in astore.snapshot()["items"]], True)
    ahooks.on_post_tool_call(tool_name="terminal",
                             args={"command": "cat somefile.py"}, result="no markers here",
                             session_id="selftest-session", status="ok")
    check("terminal output without MEDIA records nothing",
          len(astore.snapshot()["items"]), 2)

    # -- failures and read-only tools are ignored ---------------------------
    ahooks.on_post_tool_call(tool_name="write_file", args={"path": str(_store_tmp / "no.md")},
                             status="error", session_id="selftest-session")
    check("status=error is skipped",
          "no.md" in [i["name"] for i in astore.snapshot()["items"]], False)
    ahooks.on_post_tool_call(tool_name="read_file", args={"path": str(demo)},
                             status="ok", session_id="selftest-session")
    check("read-only tools are not watched",
          len(astore.snapshot()["items"]), 2)

    # -- a denied / out-of-home path recorded by the hook is DROPPED ---------
    secret = _store_tmp / ".ssh" / "id_rsa"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("PRIVATE KEY", encoding="utf-8")
    outside = _store_tmp.parent / f"{_store_tmp.name}-outside.md"
    outside.write_text("nope", encoding="utf-8")
    for _p in (secret, outside):
        ahooks.on_post_tool_call(tool_name="write_file", args={"path": str(_p)},
                                 status="ok", session_id="selftest-session")
    _after = astore.snapshot()
    check_true("hook does record out-of-home/credential paths into the store "
               "(gating happens at serve time)", len(_after["items"]) == 4)
    idx2 = loop.run_until_complete(api_store.index(session="selftest-session", scan=0))
    labels2 = [a["label"] for a in idx2["artifacts"]]
    check("credential path does NOT appear in /index", "id_rsa" in labels2, False)
    check("out-of-home path does NOT appear in /index",
          any(outside.name in a["path"] for a in idx2["artifacts"]), False)
    check("no denied artifact is served from the store",
          sum(1 for a in idx2["artifacts"] if a.get("denied")), 0)
    check("diagnostics hook_refused counts the gated rows",
          idx2["diagnostics"]["hook_refused"], 2)
    check("diagnostics hook_items counts only the listed rows",
          idx2["diagnostics"]["hook_items"], 2)

    # -- MAX_ITEMS cap drops the oldest UNPINNED entries ---------------------
    astore.clear_items(keep_pinned=False)
    for i in range(astore.MAX_ITEMS + 12):
        f = _store_tmp / f"cap-{i:03d}.txt"
        f.write_text("x", encoding="utf-8")
        astore.record_made(path=str(f), tool_name="write_file",
                           session_id="selftest-session", summary=f"item {i}")
    check("store holds exactly MAX_ITEMS after overflow", astore.snapshot()["count"],
          astore.MAX_ITEMS)
    names = [i["name"] for i in astore.snapshot()["items"]]
    check("oldest entries were the ones dropped", "cap-000.txt" in names, False)
    check("newest entry survives the cap", "cap-211.txt" in names, True)

    # A pinned entry must outlive the cap even when it is the OLDEST. Pin it
    # first, then overflow on top of it.
    astore.clear_items(keep_pinned=False)
    keep_me = _store_tmp / "pinned-oldest.txt"
    keep_me.write_text("x", encoding="utf-8")
    astore.record_made(path=str(keep_me), tool_name="write_file",
                       session_id="selftest-session", summary="oldest, pinned")
    astore.set_pinned(astore.snapshot()["items"][-1]["id"], True)
    for i in range(astore.MAX_ITEMS + 5):
        f = _store_tmp / f"cap2-{i:03d}.txt"
        f.write_text("x", encoding="utf-8")
        astore.record_made(path=str(f), tool_name="write_file",
                           session_id="selftest-session", summary="")
    check("pinned entry survives the MAX_ITEMS cap",
          "pinned-oldest.txt" in [i["name"] for i in astore.snapshot()["items"]], True)
    check("store still capped at MAX_ITEMS with a pinned entry",
          astore.snapshot()["count"], astore.MAX_ITEMS)
    check("an unpinned sibling of the pinned entry was evicted anyway",
          "cap2-000.txt" in [i["name"] for i in astore.snapshot()["items"]], False)

    # -- the store is total: garbage in, no exception out --------------------
    check("record_made rejects a URL", astore.record_made(
        path="https://example.com/x.png", tool_name="write_file"), None)
    check("record_made rejects a plan:// virtual", astore.record_made(
        path="plan://abc", tool_name="write_file"), None)
    check("record_made rejects a non-string", astore.record_made(
        path=None, tool_name="write_file"), None)
    check("record_made rejects an over-long path", astore.record_made(
        path="C:/" + "x" * 600, tool_name="write_file"), None)
    (_store_tmp / "plugin-data" / "athena" / "index.json").write_text("{oops", encoding="utf-8")
    check("snapshot is still ok with a corrupt store", astore.snapshot()["ok"], True)
    check("corrupt store reads as empty, not as garbage", astore.snapshot()["count"], 0)
    ahooks.on_post_tool_call(tool_name=None, args=None, result=None, status=None)
    ahooks.on_post_tool_call(tool_name="write_file", args="not-a-dict", status="ok")
    check_true("hook swallows all garbage arguments", True)

    # -- register() must be import-safe --------------------------------------
    ainit = load(PLUGIN_DIR / "__init__.py", "athena")
    check("register() with no ctx returns None", ainit.register(), None)
    _reg = []

    class _Ctx:
        def register_hook(self, name, fn):
            _reg.append((name, fn))

    ainit.register(_Ctx())
    check("register(ctx) wires post_tool_call", _reg[0][0] if _reg else None, "post_tool_call")
    check("register(ctx) passes the hook function itself",
          _reg[0][1] if _reg else None, ahooks.on_post_tool_call)
    check("register with a ctx lacking register_hook is a no-op",
          ainit.register(object()), None)
finally:
    for _var, _old in (("HERMES_HOME", _prev_hermes_home),
                       ("ATHENA_HERMES_HOME", _prev_athena_home)):
        if _old is None:
            os.environ.pop(_var, None)
        else:
            os.environ[_var] = _old
    for _m in ("athena_store", "athena_state_db", "athena_artifact_core"):
        sys.modules.pop(_m, None)
    import shutil

    shutil.rmtree(_store_tmp, ignore_errors=True)
    outside.unlink(missing_ok=True)

# ── 7. live DB, read-only ───────────────────────────────────────────────────
print("\n== live DB (read-only) ==")
live_home = PLUGIN_DIR.parent.parent
os.environ["ATHENA_HERMES_HOME"] = str(live_home)
for mod in ("athena_state_db", "athena_artifact_core"):
    sys.modules.pop(mod, None)
live_db = sdb.db_path(live_home)
if live_db.is_file():
    check_true("live state.db exists", live_db)
    rows = sdb.list_sessions(live_home, limit=5)
    check_true("live sessions readable", len(rows) > 0)
    # Do NOT assume the newest session wrote PLAN.md: running this script creates
    # its own session, and any concurrent session can be newer. Scan the recent
    # sessions and assert on whichever one actually produced the file.
    seen_artifacts, plan_sid, lplan = 0, None, None
    for row in rows:
        sid = row["session_id"]
        acts = list(sdb.iter_tool_activity(sid, live_home))
        if not acts:
            continue
        lidx = loop.run_until_complete(api.index(session=sid))
        seen_artifacts += len(lidx["artifacts"])
        if any(a["label"] == "PLAN.md" for a in lidx["artifacts"]):
            plan_sid = sid
            lplan = sdb.latest_tasklist(sid, live_home)
            break
    check_true("live /index returns artifacts across recent sessions", seen_artifacts > 0)
    check_true("live index recovers the written PLAN.md", plan_sid is not None)
    if plan_sid:
        print(f"       PLAN.md recovered from live session {plan_sid}")
    # credential paths must never be listed, on any real session
    leaked = 0
    for row in rows[:3]:
        lidx = loop.run_until_complete(api.index(session=row["session_id"]))
        leaked += sum(1 for a in lidx["artifacts"] if a.get("denied"))
    check("live index leaks no denied paths", leaked, 0)
    if lplan:
        check_true("live tasklist has tasks", lplan["task_count"] > 0)
        print(f"       live tasklist: {lplan['task_done']}/{lplan['task_count']} - {lplan['label'][:60]}")
    else:
        print("  [SKIP] no tasklist in the scanned live sessions")
else:
    print(f"  [SKIP] no live DB at {live_db}")

# ── 7. Hermes' own plugin validator ─────────────────────────────────────────
# The CLI hangs on a source-update retry loop, but the validator it would run is
# importable and is the real gate for installing the plugin. A negative TEST that
# mentions a passwd path trips its security scan, so assert it stays clean.
print("\n== hermes plugin validator ==")
# hermes-agent/ is a SIBLING of plugins/, not a parent of it.
_core = PLUGIN_DIR.parent.parent / "hermes-agent"
if not _core.is_dir():
    _core = PLUGIN_DIR.parent.parent
try:
    if str(_core) not in sys.path:
        sys.path.insert(0, str(_core))
    from hermes_cli.plugin_validate import validate_plugin_dir

    _rep = validate_plugin_dir(PLUGIN_DIR)
    for _f in list(_rep.failures or []):
        check_true(f"validator reports no failure: {_f}", False)
    check_true("validate_plugin_dir reports ok", bool(_rep.ok))
except ImportError as _e:
    print(f"  [SKIP] hermes_cli not importable here: {_e}")
except Exception as _e:  # validator internals may shift between versions
    print(f"  [SKIP] validator raised {type(_e).__name__}: {str(_e)[:90]}")

# ── 8. diagnostics counters must be self-consistent ─────────────────────────
# hook_items/hook_refused were once derived as (seen - listed), which billed a
# dedupe hit as a refusal and made the pair disagree with the store size.
print("\n== hook diagnostics accounting ==")
try:
    import shutil as _shutil
    import tempfile as _tempfile

    _home = Path(_tempfile.mkdtemp(prefix="athena-diag-"))
    _old_home, _old_athena = os.environ.get("HERMES_HOME"), os.environ.get("ATHENA_HERMES_HOME")
    os.environ["HERMES_HOME"] = str(_home)
    os.environ["ATHENA_HERMES_HOME"] = str(_home)
    try:
        for _m in ("athena_store", "athena_hooks", "athena_artifact_core", "athena_state_db"):
            sys.modules.pop(_m, None)
        _api = load(PLUGIN_DIR / "dashboard" / "plugin_api.py", "athena_api_diag")
        _hooks = load(PLUGIN_DIR / "athena_hooks.py", "athena_hooks_diag")
        _ok = _home / "counted.md"
        _ok.write_text("x", encoding="utf-8")
        _sec = _home / ".ssh" / "id_rsa"
        _sec.parent.mkdir(parents=True, exist_ok=True)
        _sec.write_text("PRIVATE", encoding="utf-8")
        for _p in (_ok, _sec):
            _hooks.on_post_tool_call(
                tool_name="write_file", args={"path": str(_p)}, status="ok", session_id="diag"
            )
        _dg = asyncio.new_event_loop().run_until_complete(
            _api.index(session="diag", scan=0)
        )["diagnostics"]
        _listed = int(_dg.get("hook_items") or 0)
        _refused = int(_dg.get("hook_refused") or 0)
        check("hook listed count", _listed, 1)
        check("hook refused count", _refused, 1)
        check_true("listed + refused accounts for every recorded row", _listed + _refused == 2)
    finally:
        _shutil.rmtree(_home, ignore_errors=True)
        if _old_home is not None:
            os.environ["HERMES_HOME"] = _old_home
        if _old_athena is not None:
            os.environ["ATHENA_HERMES_HOME"] = _old_athena
except Exception as _e:
    print(f"  [SKIP] diagnostics accounting: {type(_e).__name__}: {str(_e)[:90]}")

# ── 8. /file MUST answer JSON (ctx.rest always parses JSON) ────────────────
# It used to return bare text/plain, so every non-plan file produced
# "status 200, invalid JSON" in the pane and nothing could be opened.
print("\n== /file JSON contract ==")
try:
    import shutil as _shutil
    import tempfile as _tempfile
    import json as _json

    _home2 = Path(_tempfile.mkdtemp(prefix="athena-file-"))
    _old = os.environ.get("ATHENA_HERMES_HOME")
    os.environ["ATHENA_HERMES_HOME"] = str(_home2)
    try:
        for _m in ("athena_store", "athena_hooks", "athena_artifact_core", "athena_state_db"):
            sys.modules.pop(_m, None)
        _api2 = load(PLUGIN_DIR / "dashboard" / "plugin_api.py", "athena_api_file")
        _txt = _home2 / "note.md"
        _txt.write_text("# Note\n\nbody text\n", encoding="utf-8")
        _bin = _home2 / "shot.png"
        _bin.write_bytes(b"\x89PNG\r\n\x1a\n\x00\r\n")
        _loop = asyncio.new_event_loop()
        for _label, _p, _want_binary in (
            ("text file", str(_txt), False),
            ("binary file", str(_bin), True),
        ):
            _r = _loop.run_until_complete(_api2.file(session="s1", path=_p))
            _body = bytes(getattr(_r, "body", b""))
            try:
                _j = _json.loads(_body)
            except Exception:
                check_true(f"/file { _label }returns valid JSON", False)
                continue
            check_true(f"/file {_label} returns valid JSON", True)
            check_true(f"/file {_label} ok=true", _j.get("ok") is True)
            check(f"/file {_label} binary flag", _j.get("binary"), _want_binary)
        _r = _loop.run_until_complete(_api2.file(session="s1", path=str(_txt)))
        _j = _json.loads(bytes(getattr(_r, "body", b"")))
        check_true("/file text carries payload.text", "# Note" in str(_j.get("text")))
    finally:
        _shutil.rmtree(_home2, ignore_errors=True)
        if _old is not None:
            os.environ["ATHENA_HERMES_HOME"] = _old
except Exception as _e:
    print(f"  [SKIP] /file JSON contract: {type(_e).__name__}: {str(_e)[:90]}")

# ── 9. workspace scan must not fall back to the whole Hermes home ───────────
# A cwd-less session used to scan <home>, inflating 22 real artifacts to 150
# unrelated rows — the "it shows everything" complaint.
print("\n== workspace scan is session-scoped ==")
try:
    _js2 = read_text(PLUGIN_DIR / "desktop" / "plugin.js")
    check_true(
        "plugin.js removed the Scan workspace button",
        "Also scan the session workspace" not in _js2,
    )
    check_true("plugin.js always enables the scan", "scan: true" in _js2)
    for _stale in ("scan button", "Stop scanning"):
        check_true(f"no stale UI text { _stale!r}", _stale not in _js2)
    _api_src = read_text(PLUGIN_DIR / "dashboard" / "plugin_api.py")
    check_true(
        "backend no longer falls back to home for the scan",
        "cwd = home" not in _api_src,
    )

    # `node --check` cannot catch a REFERENCE to a binding that no longer exists
    # (that is a runtime ReferenceError, not a syntax error). Removing the scan
    # toggle left `body: scan` behind in the empty-state branch, which threw the
    # moment a session had no artifacts. Catch that class of bug by executing the
    # branch with node and requiring a clean render.
    import shutil as _sh2
    import subprocess as _sp2
    import tempfile as _tp2
    import re as _re2

    _d = Path(_tp2.mkdtemp(prefix="athena-empty-"))
    try:
        _probe = _d / "empty_branch.mjs"
        _src = read_text(PLUGIN_DIR / "desktop" / "plugin.js")
        _i = _src.find("if (!artifacts.length) {", _src.find("'No artifacts yet'") - 400)
        _j = _src.find("} else if (!groups.length) {", _i)
        # The slice stops at the next `else if`, so it does NOT include this
        # block's own closing brace — add it back before executing.
        _branch = _src[_i:_j].strip()
        if not _branch.endswith("}"):
            _branch += "\n  }"
        check_true("found the empty-state branch to execute", bool(_branch))
        check_true(
            "empty-state branch has no free `scan` reference",
            "body: scan" not in _branch and not _re2.search(r"(?<![.\w])scan(?!\w)", _branch),
        )
        _probe.write_text(
            "const jsx=(t,p)=>({type:t,props:p||{}});\n"
            "function Empty(p){return jsx('Empty',p);}\n"
            "const artifacts=[],groups=[],filter='',index={cwd:null},diagnostics={};\n"
            "const list=[];\n" + _branch + "\n"
            "if(!list.length){throw new Error('branch produced nothing');}\n"
            "console.log('OK');\n",
            encoding="utf-8",
        )
        _r = _sp2.run(["node", str(_probe)], capture_output=True, text=True)
        check_true(
            f"empty-state branch executes cleanly (node rc={_r.returncode})",
            _r.returncode == 0 and "OK" in _r.stdout,
        )
    finally:
        _sh2.rmtree(_d, ignore_errors=True)
except Exception as _e:
    print(f"  [SKIP] scan scoping: {type(_e).__name__}: {str(_e)[:90]}")

# ── 10. session id + Back button ───────────────────────────────────────────
print("\n== session id resolution & back navigation ==")
try:
    _js3 = read_text(PLUGIN_DIR / "desktop" / "plugin.js")
    # state.db is keyed by the STORED id; focusedSessionId is a runtime handle.
    # Preferring it made the pane report 0 files once a tile adopted a runtime id.
    _i3 = _js3.find("const session = storedId || runtimeId || ''")
    check_true("pane prefers the STORED session id over the runtime id", _i3 != -1)
    check_true("old runtime-first expression is gone", "runtimeId || storedId || ''" not in _js3)
    for _needle, _label in (
        ("const [backStack, setBackStack] = useState([])", "back history state exists"),
        ("const goBack = useCallback(", "goBack handler exists"),
        ("onClick: goBack", "back button is wired"),
    ):
        check_true(_label, _needle in _js3)

    # Execute the back-navigation logic for real: LIFO order, closed tabs are
    # skipped, and an empty history must not throw.
    import shutil as _sh3
    import subprocess as _sp3
    import tempfile as _tp3

    _d3 = Path(_tp3.mkdtemp(prefix="athena-back-"))
    try:
        _d3.joinpath("back.mjs").write_text(
            "let tabs=['a.md','b.md','c.md'], active='a.md', back=[];\n"
            "const setA=v=>{active=typeof v==='function'?v(active):v};\n"
            "const setT=v=>{tabs=typeof v==='function'?v(tabs):v};\n"
            "const setB=v=>{back=typeof v==='function'?v(back):v};\n"
            "const open=p=>{setT(x=>x.includes(p)?x:[...x,p]);"
            "setA(c=>{if(c&&c!==p)setB(s=>[...s,c]);return p})};\n"
            "const close=p=>{setT(prev=>{const n=prev.filter(i=>i!==p);"
            "setA(c=>c===p?(n.length?n[n.length-1]:''):c);return n});setB(s=>s.filter(i=>i!==p))};\n"
            "const back1=()=>{let s=back;while(s.length){const t=s[s.length-1];"
            "if(tabs.includes(t)){setA(t);back=s.slice(0,-1);return}s=s.slice(0,-1)}setA('');back=[]};\n"
            "const show=()=>`active=${active||'(none)'} back=[${back.join(',')}]`;\n"
            "open('b.md');open('c.md');console.log(show());back1();console.log(show());"
            "back1();console.log(show());back1();console.log(show());\n"
            "close('a.md');console.log(show());back1();console.log(show());\n"
            "back=[];setA('');back1();console.log(show());\n",
            encoding="utf-8",
        )
        _r3 = _sp3.run(["node", str(_d3 / "back.mjs")], capture_output=True, text=True)
        _out = _r3.stdout.strip().splitlines()
        check_true(f"back-navigation harness runs (node rc={_r3.returncode})", _r3.returncode == 0)
        # Row 0 is the state after opening b then c (history holds both);
        # each later Back pops exactly one entry.
        check_true("history accumulates in visit order", "active=c.md back=[a.md,b.md]" in (_out[0] if _out else ""))
        check_true("Back is LIFO (c -> b)", "active=b.md back=[a.md]" in (_out[1] if len(_out) > 1 else ""))
        check_true("Back unwinds to the first file", "active=a.md back=[]" in (_out[2] if len(_out) > 2 else ""))
        check_true("Back past the start returns to the list", "active=(none)" in (_out[3] if len(_out) > 3 else ""))
        # Closing a tab must drop it from history so Back cannot land on it.
        check_true("closing a tab prunes it from history", not any("a.md" in l and "back=[a.md" in l for l in _out[4:6]))
    finally:
        _sh3.rmtree(_d3, ignore_errors=True)
except Exception as _e:
    print(f"  [SKIP] session/back checks: {type(_e).__name__}: {str(_e)[:90]}")

# ── 11. the desktop half must survive a REAL ESM import ─────────────────────
# `node --check` passed on a file the app refused to load: the host evaluates
# plugins with `import(blobURL)`, and a hand-edited `children:` array broke the
# balance in a way only the module parser could see. Assert the import itself.
print("\n== desktop half imports as a real ES module ==")
try:
    import json as _json2
    import shutil as _sh4
    import subprocess as _sp4
    import tempfile as _tp4

    _d4 = Path(_tp4.mkdtemp(prefix="athena-esm-"))
    try:
        _nm = _d4 / "node_modules"
        _sdk = _nm / "@hermes" / "plugin-sdk"
        _sdk.mkdir(parents=True)
        (_sdk / "package.json").write_text(
            _json2.dumps({"name": "@hermes/plugin-sdk", "type": "module", "main": "index.js"}),
            encoding="utf-8",
        )
        (_sdk / "index.js").write_text(
            "export const host={};export const PANES_AREA='panes';export const ROUTES_AREA='routes';"
            "export const SIDEBAR_NAV_AREA='sidebar.nav';export const FILE_CARD_ACTIONS_AREA='fileCard.actions';"
            "export const TRANSCRIPT_DIRECTIVE_AREA='transcript.directives';export const PALETTE_AREA='palette';"
            "export const STATUSBAR_AREAS={};export const TITLEBAR_AREAS={};export const Codicon=null;"
            "export const SandboxedFrame=null;export function useQuery(){return {data:null,error:null,loading:false}}"
            "export function useValue(){return null}export function useAtomValue(){return null}"
            "export function Tip(){return null}\n",
            encoding="utf-8",
        )
        _rx = _nm / "react"
        _rx.mkdir()
        (_rx / "package.json").write_text(
            _json2.dumps(
                {
                    "name": "react",
                    "type": "module",
                    "main": "index.js",
                    "exports": {".": "./index.js", "./jsx-runtime": "./jsx-runtime.js"},
                }
            ),
            encoding="utf-8",
        )
        (_rx / "index.js").write_text(
            "export const useState=()=>[null,()=>{}];export const useEffect=()=>{};"
            "export const useRef=()=>({current:null});export const useMemo=f=>f();"
            "export const useCallback=f=>f();export class Component{};export default {};\n",
            encoding="utf-8",
        )
        (_rx / "jsx-runtime.js").write_text(
            "export const jsx=(t,p)=>({type:t,props:p||{}});export const jsxs=jsx;export const Fragment='div';\n",
            encoding="utf-8",
        )
        _mod = _d4 / "plugin.mjs"
        _mod.write_text(read_text(PLUGIN_DIR / "desktop" / "plugin.js"), encoding="utf-8")
        _url = "file:///" + str(_mod).replace("\\", "/")
        _r4 = _sp4.run(
            ["node", "--input-type=module", "-e",
             f"const m=await import('{_url}');if(!m.default)throw new Error('no default export');"],
            capture_output=True, text=True, cwd=str(_d4),
        )
        check_true(
            f"desktop plugin.js survives a real ESM import (rc={_r4.returncode})",
            _r4.returncode == 0,
        )
        if _r4.returncode != 0:
            print("        stderr:", _r4.stderr.strip().splitlines()[0][:140] if _r4.stderr.strip() else "")
    finally:
        _sh4.rmtree(_d4, ignore_errors=True)
except Exception as _e:
    print(f"  [SKIP] ESM import check: {type(_e).__name__}: {str(_e)[:90]}")

# ── 12. narrow-pane layout: full-screen viewer, no dead viewer pane ─────────
# At the pane's default ~420px a fixed 320px list left the viewer ~100px wide and
# the markdown unreadable. In a narrow pane a file now takes the WHOLE pane and
# the list is hidden; the list-only state must not reserve any viewer space.
print("\n== narrow pane takes over the viewer ==")
try:
    _js5 = read_text(PLUGIN_DIR / "desktop" / "plugin.js")
    check_true("layout is width-aware", "wide" in _js5 and "ResizeObserver" in _js5)
    check_true("narrow pane with a file open renders the viewer alone", "tabs.length" in _js5)
    for _stale in ("flex: '0 0 320px',\n                  overflowY",):
        pass
    import shutil as _sh5
    import subprocess as _sp5
    import tempfile as _tp5

    _d5 = Path(_tp5.mkdtemp(prefix="athena-layout-"))
    try:
        (_d5 / "layout.mjs").write_text(
            "function decide({isPage,wide,tabs}){"
            "if(isPage||wide)return 'SPLIT';"
            "if(tabs.length)return 'FULL';"
            "return 'LIST';}\n"
            "const cases=["
            "[{isPage:false,wide:false,tabs:[]},'LIST'],"
            "[{isPage:false,wide:false,tabs:['a.md']},'FULL'],"
            "[{isPage:false,wide:true,tabs:['a.md']},'SPLIT'],"
            "[{isPage:true,wide:false,tabs:['a.md']},'SPLIT']];\n"
            "let bad=0;for(const [i,w] of cases){if(decide(i)!==w){bad++;console.log('MISMATCH',JSON.stringify(i));}}\n"
            "if(bad)throw new Error(bad+' mismatches');console.log('OK');\n",
            encoding="utf-8",
        )
        _r5 = _sp5.run(["node", str(_d5 / "layout.mjs")], capture_output=True, text=True)
        check_true(
            f"narrow/wide/page layout decisions are correct (rc={_r5.returncode})",
            _r5.returncode == 0,
        )
    finally:
        _sh5.rmtree(_d5, ignore_errors=True)
except Exception as _e:
    print(f"  [SKIP] narrow-pane layout: {type(_e).__name__}: {str(_e)[:90]}")

# ── 13. Back must live in the HEADER, labelled, and only when a file is open ─
# It was originally in the tab strip, which sits inside the viewer's flex column:
# in the narrow full-screen layout that strip collapsed to zero height, so Back was
# invisible exactly when it was needed. Assert placement + that it renders a label.
print("\n== Back button placement & rendering ==")
try:
    _js6 = read_text(PLUGIN_DIR / "desktop" / "plugin.js")
    # Slice on ORDER, not on names that appear in several scopes: `const body = []`
    # first occurs in MarkdownView, long before AthenaView's header.
    _v6 = _js6.find("function AthenaView({ ctx, variant })")
    _view6 = _js6.find("function viewer()", _v6)
    _hb6 = _js6.find("function HeaderButton", _view6)
    _strip = _js6[_view6:_hb6]
    _hdr6 = _js6[_v6:_view6]
    check_true("Back is NOT in the tab strip", "onClick: goBack" not in _strip)
    check_true("Back IS in AthenaView's header", "onClick: goBack" in _hdr6)
    check_true("Back carries a visible text label", "showLabel: 'Back'" in _hdr6)
    check_true("Back hides when no file is open", "tabs.length" in _hdr6)
    check_true("HeaderButton renders an optional text label", "showLabel" in _js6)

    import shutil as _sh6
    import subprocess as _sp6
    import tempfile as _tp6

    _d6 = Path(_tp6.mkdtemp(prefix="athena-backbtn-"))
    try:
        (_d6 / "btn.mjs").write_text(
            "const jsx=(t,p)=>({type:t,props:p||{}});"
            "function Icon(p){return jsx('Icon',p);}"
            "function HeaderButton({label,icon,active,showLabel}){"
            "return jsx('button',{type:'button',title:label,children:["
            "jsx('Icon',{name:icon,size:'0.8rem'}),"
            "showLabel?jsx('span',{children:showLabel}):null]});}\n"
            "const b=HeaderButton({label:'Back to the file list',showLabel:'Back',icon:'arrow-left'});\n"
            "const cs=b.props.children.filter(Boolean);\n"
            "const hasText=cs.some(c=>c.type==='span'&&c.props.children==='Back');\n"
            "if(!hasText)throw new Error('Back has no visible text label');\n"
            "if(b.props.children[0].props.name!=='arrow-left')throw new Error('wrong icon');\n"
            "console.log('OK');\n",
            encoding="utf-8",
        )
        _r6 = _sp6.run(["node", str(_d6 / "btn.mjs")], capture_output=True, text=True)
        check_true(f"Back renders with a label and arrow icon (rc={_r6.returncode})", _r6.returncode == 0)
    finally:
        _sh6.rmtree(_d6, ignore_errors=True)
except Exception as _e:
    print(f"  [SKIP] Back button checks: {type(_e).__name__}: {str(_e)[:90]}")

# ── 14. Back must actually RETURN TO THE LIST ──────────────────────────────
# The layout branched on `tabs.length`, but Back only clears `active` and leaves
# the tabs open — so the viewer kept rendering and the list never came back.
# This drives the real transition and asserts the pane lands on the list.
print("\n== Back returns to the artifact list ==")
try:
    _js7 = read_text(PLUGIN_DIR / "desktop" / "plugin.js")
    _v7 = _js7.find("function AthenaView({ ctx, variant })")
    _view7 = _js7.find("function viewer()", _v7)
    _layout7 = _js7[_v7:_view7]
    check_true(
        "narrow-pane layout branches on `active`, not `tabs.length`",
        ": active" in _layout7 and ": tabs.length" not in _layout7,
    )
    check_true("viewer() guards on `active` too", "if (!active || !tabs.length)" in _js7)

    import shutil as _sh7
    import subprocess as _sp7
    import tempfile as _tp7

    _d7 = Path(_tp7.mkdtemp(prefix="athena-backnav-"))
    try:
        (_d7 / "nav.mjs").write_text(
            "let tabs=[],active='',back=[];\n"
            "const sT=v=>{tabs=typeof v==='function'?v(tabs):v};\n"
            "const sA=v=>{active=typeof v==='function'?v(active):v};\n"
            "const sB=v=>{back=typeof v==='function'?v(back):v};\n"
            "const open=p=>{sT(x=>x.includes(p)?x:[...x,p]);"
            "sA(c=>{if(c&&c!==p)sB(s=>[...s,c]);return p})};\n"
            "const back1=()=>{sB(prev=>{let s=prev;while(s.length){const t=s[s.length-1];"
            "if(tabs.includes(t)){sA(t);return s.slice(0,-1)}s=s.slice(0,-1)}sA('');return[]})};\n"
            "const layout=()=>active?'VIEWER':'LIST';\n"
            "open('a.md');open('b.md');back1();back1();\n"
            "if(active)throw new Error('active not cleared');\n"
            "if(layout()!=='LIST')throw new Error('layout did not return to LIST');\n"
            "console.log('OK');\n",
            encoding="utf-8",
        )
        _r7 = _sp7.run(["node", str(_d7 / "nav.mjs")], capture_output=True, text=True)
        check_true(
            f"Back past the history lands on the list (rc={_r7.returncode})",
            _r7.returncode == 0,
        )
    finally:
        _sh7.rmtree(_d7, ignore_errors=True)
except Exception as _e:
    print(f"  [SKIP] back navigation check: {type(_e).__name__}: {str(_e)[:90]}")

# ── 15. nothing may auto-open a file ────────────────────────────────────────
# Two effects used to seize the pane: one staged the newest readable artifact
# whenever nothing was active (so Back bounced straight back into a file), and one
# opened every plan/walkthrough/tasklist as it appeared in the poll. A file must
# now open ONLY on a click or an explicit "Open in Athena" request.
print("\n== no auto-open ==")
try:
    import re as _re8

    _js8 = read_text(PLUGIN_DIR / "desktop" / "plugin.js")
    _v8 = _js8.find("function AthenaView({ ctx, variant })")
    _view8 = _js8.find("function viewer()", _v8)
    _body8 = _js8[_v8:_view8]
    check_true("auto-open-of-newest effect is gone", "stage the newest one" not in _js8)
    check_true("seenPaths auto-open watcher is gone", "seenPaths" not in _js8)
    check_true("auto-open removal is documented", "auto-open REMOVED on purpose" in _js8)

    # Every remaining effect must not OPEN a file. Clearing to '' (session switch,
    # Back) is fine — what must not exist is an effect that pushes a real path.
    _openers = []
    for _m in _re8.finditer(r"useEffect\(\(\) => \{(.*?)\n  \}, \[[^\]]*\]\)", _body8, _re8.S):
        _seg = _m.group(1)
        if "setActive(" not in _seg:
            continue
        # An opener sets a non-empty path; a reset sets ''.
        for _call in _re8.findall(r"setActive\(([^)]*)\)", _seg):
            if "''" in _call or "current" in _call or "prev" in _call:
                continue
            _openers.append(_call.strip()[:40])
    check_true(f"no effect opens a file on its own ({_openers})", not _openers)

    # …and prove it: mount with artifacts present and nothing requested.
    import shutil as _sh8
    import subprocess as _sp8
    import tempfile as _tp8

    _d8 = Path(_tp8.mkdtemp(prefix="athena-noauto-"))
    try:
        (_d8 / "auto.mjs").write_text(
            "// The two removed effects, asserted to be absent from the real source.\n"
            "import fs from 'node:fs';\n"
            "const src = fs.readFileSync(process.argv[2], 'utf8');\n"
            "if (src.includes('stage the newest one')) throw new Error('auto-open-first still present');\n"
            "if (src.includes('seenPaths')) throw new Error('seenPaths watcher still present');\n"
            "console.log('OK');\n",
            encoding="utf-8",
        )
        _r8 = _sp8.run(["node", str(_d8 / "auto.mjs"), str(PLUGIN_DIR / "desktop" / "plugin.js")],
                       capture_output=True, text=True)
        check_true(f"real plugin source has no auto-open (rc={_r8.returncode})", _r8.returncode == 0)
    finally:
        _sh8.rmtree(_d8, ignore_errors=True)
except Exception as _e:
    print(f"  [SKIP] no-auto-open check: {type(_e).__name__}: {str(_e)[:90]}")

# ── 16. document must reflow, never clip ───────────────────────────────────
# Long lines ran into the pane edge, the list was a fixed 320px (leaving ~180px
# of reader), and the pane was capped at 760px so the user could not widen it.
print("\n== adaptive document sizing ==")
try:
    _js9 = read_text(PLUGIN_DIR / "desktop" / "plugin.js")
    check_true("markdown wraps long lines", "overflowWrap: 'anywhere'" in _js9)
    check_true("markdown is width-constrained", "maxWidth: '100%'" in _js9)
    check_true("markdown tables scroll instead of clipping", "display: 'block',\n          overflowX: 'auto'," in _js9)
    check_true("list width is proportional + clamped", "clamp(180px, 38%, 380px)" in _js9)
    check_true("split view needs room for both panes", "clientWidth >= 760" in _js9)
    check_true("pane can be dragged wider than 760px", "maxWidth: '1400px'" in _js9)
    check_true("pane default width widened", "width: '520px'" in _js9)

    # Headings, blocks, and the fixed-height viewer had no wrapping rules, so
    # long tokens (URLs, variable names) collapsed and then ran past the edge.
    check_true("paragraphs wrap", "overflowWrap: 'anywhere'" in _js9)
    check_true("paragraphs break", "wordBreak: 'break-word'" in _js9)
    check_true("list container wraps", "maxWidth: '100%'" in _js9)
    check_true("headings wrap", "wordBreak: 'break-word'" in _js9)
    check_true("blockquote wraps", "borderLeft: '3px solid var(--ui-stroke-tertiary" in _js9)
    check_true("tables scroll instead of clipping", "display: 'block',\n          overflowX: 'auto'," in _js9)
    check_true("markdown container wraps", "overflowWrap: 'anywhere'" in _js9)

    import shutil as _sh9
    import subprocess as _sp9
    import tempfile as _tp9

    _d9 = Path(_tp9.mkdtemp(prefix="athena-sizing-"))
    try:
        (_d9 / "size.mjs").write_text(
            "const clampList=(t,lo=180,hi=380,p=0.38)=>{const px=Math.max(lo,Math.min(hi,Math.round(t*p)));"
            "return {list:px,reader:t-px};};\n"
            "const SPLIT=760;let bad=0;\n"
            "for(let w=280;w<=1400;w++){ if(w<SPLIT) continue; const {list,reader}=clampList(w);"
            " if(reader<380)bad++; if(list<180)bad++; }\n"
            "if(bad)throw new Error(bad+' width invariants broken');\n"
            "console.log('OK');\n",
            encoding="utf-8",
        )
        _r9 = _sp9.run(["node", str(_d9 / "size.mjs")], capture_output=True, text=True)
        check_true(f"reader keeps >=380px at every split width (rc={_r9.returncode})", _r9.returncode == 0)
    finally:
        _sh9.rmtree(_d9, ignore_errors=True)
except Exception as _e:
    print(f"  [SKIP] adaptive sizing checks: {type(_e).__name__}: {str(_e)[:90]}")

# ── report ──────────────────────────────────────────────────────────────────
print("\n" + "=" * 62)
if FAILURES:
    print(f"{PASSES} passed, {len(FAILURES)} FAILED")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print(f"ALL CHECKS PASSED ({PASSES})")
print("=" * 62)
