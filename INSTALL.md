# Install — Athena

## Requirements

- Hermes Desktop with plugin support
- Python 3.11+ for the backend half (`fastapi` comes from Hermes itself)
- Node (only for `node --check` during verification)
- Backend running in **dashboard** mode — see the warning below

## Layout

```
plugins/athena/
  plugin.yaml              manifest v2, provides post_tool_call
  __init__.py              register(ctx) -> wires the hook (import-safe when bare)
  athena_hooks.py          post_tool_call observer: which tools produce paths
  athena_store.py          live JSON store of produced paths (atomic, capped)
  dashboard/
    manifest.json          mounts plugin_api.py
    plugin_api.py          /health /index /file /events  (+ the store merge)
  state_db.py              read-only SQLite transcript reader (BACKFILL)
  artifact_core.py         classification + credential denial policy
  desktop/plugin.js        CANONICAL desktop half (edit here)
  selftest.py              offline verification
```

The copy the app actually loads is `desktop-plugins/athena/plugin.js`.
`plugins/athena/desktop/plugin.js` is canonical — **edit that, then copy**:

```bash
cp plugins/athena/desktop/plugin.js desktop-plugins/athena/plugin.js
```

## Steps

1. Confirm the plugin is present:
   ```bash
   ls D:/Wafig/Hermes/plugins/athena
   ```

2. Enable it and confirm status:
   ```bash
   hermes plugins enable athena
   hermes plugins show athena      # expect "Status: enabled"
   ```

3. Sync the desktop half:
   ```bash
   cp plugins/athena/desktop/plugin.js desktop-plugins/athena/plugin.js
   ```

4. Reload desktop plugins in the app: <kbd>⌘K</kbd> → *Reload desktop plugins*

   **Required after every plugin change.** The backend module is cached in the
   running process, so an edited `plugin_api.py`, `__init__.py` or
   `athena_hooks.py` does not take effect — and the `post_tool_call` hook does
   not attach — until this reload (or a Hermes restart). Without it, `/index`
   keeps serving the pre-change code and the store stays empty.

5. Open a session, then open Athena from the right pane, the sidebar, or `/athena`.

## ⚠ Dashboard mode is required

A headless `hermes serve` sets `HERMES_SERVE_HEADLESS=1`, which **unmounts
plugin API routes**. Athena's pane then shows:

```
404: {'error': "Headless backend (hermes serve): web UI disabled - use `hermes dashboard' for the browser UI."}
```

Fix: run the backend with `hermes dashboard` (or any non-headless web mode),
then reload desktop plugins. This is a backend-mode issue, **not** an Athena bug.

## Verify

```bash
cd D:/Wafig/Hermes/plugins/athena
python selftest.py                 # 178 checks; ALL CHECKS PASSED
node --check desktop/plugin.js     # silent on success
hermes plugins show athena
```

Optional live check against a running dashboard:

```bash
TOKEN=<the app's session token>
curl -s -H "X-Hermes-Session-Token: $TOKEN" \
  "http://127.0.0.1:<port>/api/plugins/athena/health"
curl -s -H "X-Hermes-Session-Token: $TOKEN" \
  "http://127.0.0.1:<port>/api/plugins/athena/index?session=<session-id>"
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `404 Headless backend ...` | backend is headless; use `hermes dashboard` |
| Pane shows **"No artifacts yet"** | the pane is querying a session with no transcript rows. Open Diagnostics in the Athena header to see the resolved `state.db` path. |
| Task list never appears | no `todo`/`plan_create` call yet in this session — Athena synthesizes from the newest one, it does not create them |
| Plan file not listed | it lives outside `<hermes_home>`; by design Athena only indexes inside the home |
| Desktop blank | reload desktop plugins again; confirm the copy is byte-identical to the source |
| Changes not appearing | live updates poll every **15s**; wait one cycle |
| Edits have no effect at all | the backend module is cached — run **Reload desktop plugins** (<kbd>⌘K</kbd>) or restart Hermes |
| Artifact index stays empty / `hook_items` is 0 | the `post_tool_call` hook never attached. `plugin.yaml` must list `provides_hooks: [post_tool_call]`, and you must have reloaded desktop plugins. Check `diagnostics.store` for the resolved JSON path. |
| A file you wrote is missing from the pane | `/index` drops anything outside the Hermes home, credential paths (`.ssh`, `id_rsa`, …), and toolchain noise. `diagnostics.hook_refused` counts what was dropped and why. |
| Store file is huge / stale entries | bounded at `MAX_ITEMS = 200`; it drops the oldest **unpinned** entries first. Delete `<hermes_home>/plugin-data/athena/index.json` to reset — the transcript reader still backfills. |
