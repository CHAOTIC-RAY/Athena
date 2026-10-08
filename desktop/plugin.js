/**
 * Athena — desktop frontend.
 *
 * A RIGHT-SIDE pane, docked beside FILES, showing the artifacts the FOCUSED
 * session produced (markdown docs, walkthroughs, plans, code, data, images) and
 * opening them IN the panel: a tab strip, a rendered markdown reader, a code
 * view, image display and a sandboxed HTML preview. The same component renders
 * as a full page route and from a sidebar nav row.
 *
 * WHY THIS PLUGIN IS SHAPED THE WAY IT IS
 * ---------------------------------------
 * 1. NO HOOKS. The plugin this replaces recorded its index from `post_tool_call`
 *    hooks; the live runtime reported `Listens: (none)` for it, so it recorded
 *    nothing and its pane was permanently empty. Athena's data comes from the
 *    read-only dashboard routes `/index`, `/file`, `/health`, which derive the
 *    list from the transcript Hermes already persists.
 *
 * 2. NO NAMED SDK IMPORTS. The runtime evaluates this file as an ES module, so a
 *    named import that the shipped bundle does not export is a *fatal* module
 *    error, not a soft failure. Every SDK member is therefore taken from the
 *    namespace object and feature-detected; each one has a working fallback, and
 *    the contribution area strings are duplicated as literals.
 *
 * 3. NO useQuery / useValue. Data fetching, atom subscription and markdown
 *    rendering are implemented locally on top of `react` alone (the only
 *    specifier besides the SDK that the loader maps). `Streamdown` is used for
 *    markdown when the bundle exposes it, behind an error boundary that falls
 *    back to the local renderer.
 *
 * 4. NOTHING THROWS OUT OF `register`, a render, or an event handler — every
 *    failure degrades to a visible message.
 *
 * DATA CHANNEL
 *   `ctx.rest(path)` resolves to `/api/plugins/athena<path>` (namespace-scoped,
 *   so the core `/api/sessions/*` and `/api/fs/*` routes are NOT reachable from
 *   here — that is why the Python half proxies them).
 */

import * as SDK from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { Component, useCallback, useEffect, useMemo, useRef, useState } from 'react'

const ID = 'athena'
/** A contributed pane's id in the layout tree is `<pluginId>:<paneId>`. */
const PANE_ID = `${ID}:pane`
const PAGE_PATH = '/athena'
const POLL_MS = 15_000
const MAX_TEXT_RENDER = 400_000

// ── area constants (literal duplicates of the SDK constants) ────────────────
const PANES_AREA = SDK.PANES_AREA || 'panes'
const ROUTES_AREA = SDK.ROUTES_AREA || 'routes'
const SIDEBAR_NAV_AREA = SDK.SIDEBAR_NAV_AREA || 'sidebar.nav'
const FILE_CARD_ACTIONS_AREA = SDK.FILE_CARD_ACTIONS_AREA || 'fileCard.actions'
const TRANSCRIPT_DIRECTIVE_AREA = SDK.TRANSCRIPT_DIRECTIVE_AREA || 'transcript.directives'

const host = SDK.host || {}

/**
 * Client-side guard for a path arriving from untrusted model output.
 *
 * Deliberately conservative: the backend already enforces the real rule (the
 * path must resolve inside the Hermes home, and credential paths are refused),
 * so this only needs to reject obvious garbage before we offer a chip at all.
 */
function isSafeOpenPath(path) {
  const value = typeof path === 'string' ? path.trim() : ''
  if (!value || value.length > 1024) return false
  // Absolute (POSIX or Windows drive/UNC), never a traversal or a URI scheme.
  if (!/^(?:[a-z]:[\\/]|[\\/])/i.test(value)) return false
  if (value.includes('\0')) return false
  const segments = value.replace(/\\/g, '/').split('/')
  if (segments.some(part => part === '..')) return false
  const deniedDirs = ['.ssh', '.aws', '.gnupg', '.azure', '.kube', 'vault', 'mcp-tokens']
  const lowered = segments.map(part => part.toLowerCase())
  if (lowered.some(part => deniedDirs.includes(part))) return false
  if (/^file:|^https?:/i.test(value)) return false
  return true
}
const Codicon = SDK.Codicon || null
const SandboxedFrame = SDK.SandboxedFrame || null
const Streamdown = SDK.Streamdown || null

// ── optional-host shims ─────────────────────────────────────────────────────
const readAtom = value => ({
  get: () => value,
  listen: () => () => {},
  subscribe: () => () => {},
})

const focusedSessionAtom =
  (host.state && host.state.focusedSessionId) || readAtom(null)
const focusedStoredSessionAtom =
  (host.state && host.state.focusedStoredSessionId) || readAtom(null)
const focusedProfileAtom =
  (host.state && host.state.focusedSessionProfile) || readAtom(null)

/** Subscribe to a nanostores-style atom without importing the SDK's hook. */
function useAtomValue(atom) {
  const [, bump] = useState(0)

  useEffect(() => {
    if (!atom || typeof atom.get !== 'function') {
      return undefined
    }
    const notify = () => bump(n => n + 1)
    let off = null
    try {
      if (typeof atom.listen === 'function') {
        off = atom.listen(notify)
      } else if (typeof atom.subscribe === 'function') {
        off = atom.subscribe(notify)
      }
    } catch {
      off = null
    }
    return () => {
      if (typeof off === 'function') {
        try {
          off()
        } catch {
          /* disposal is best-effort */
        }
      }
    }
  }, [atom])

  try {
    return atom && typeof atom.get === 'function' ? atom.get() : null
  } catch {
    return null
  }
}

function rest(ctx, path, options) {
  if (!ctx || typeof ctx.rest !== 'function') {
    return Promise.reject(new Error('Plugin transport unavailable'))
  }
  return ctx.rest(path, options)
}

/** Small polling fetch hook — deliberately not the SDK's useQuery. */
function useRest(ctx, path, { enabled = true, interval = 0, key = '' } = {}) {
  const [state, setState] = useState({ data: null, error: '', loading: Boolean(enabled) })
  const [nonce, setNonce] = useState(0)

  useEffect(() => {
    if (!enabled || !path) {
      setState({ data: null, error: '', loading: false })
      return undefined
    }
    let live = true
    let timer = null

    const run = async () => {
      try {
        const data = await rest(ctx, path)
        if (!live) return
        setState({ data, error: '', loading: false })
      } catch (err) {
        if (!live) return
        setState(prev => ({
          data: prev.data,
          error: err && err.message ? err.message : 'Request failed',
          loading: false,
        }))
      }
    }

    setState(prev => ({ ...prev, loading: true }))
    void run()
    if (interval > 0) {
      timer = setInterval(run, interval)
    }
    return () => {
      live = false
      if (timer) clearInterval(timer)
    }
  }, [ctx, path, enabled, interval, nonce, key])

  const refresh = useCallback(() => setNonce(n => n + 1), [])
  return { ...state, refresh }
}

/**
 * Live updates come from POLLING `/index`, not SSE.
 *
 * Why not `EventSource`: the plugin renderer does not guarantee the browser
 * EventSource global, and the only transport this plugin is given is
 * `ctx.rest(path)`, which returns a *Promise* — never a URL string. Handing
 * that Promise to `new EventSource(...)` could never connect and only
 * produced a ReferenceError at runtime. Polling through the documented
 * transport is the only shape that actually works here.
 */
function useLiveSessionIndex(ctx, session, options = {}) {
  const scan = Boolean(options.scan)
  const profile = options.profile || ''
  return useRest(
    ctx,
    session
      ? `/index?session=${encodeURIComponent(session)}${scan ? '&scan=1' : ''}${
          profile ? `&profile=${encodeURIComponent(profile)}` : ''
        }`
      : '',
    { enabled: Boolean(session), interval: POLL_MS, key: `${session}|${scan}|${profile}` },
  )
}

// ── helpers ─────────────────────────────────────────────────────────────────
function Icon({ name, size = '0.85rem' }) {
  if (Codicon) {
    return jsx(Codicon, { name, size })
  }
  return jsx('span', { style: { fontSize: size, lineHeight: 1 }, children: '\u2022' })
}

function humanSize(bytes) {
  const n = Number(bytes)
  if (!Number.isFinite(n) || n <= 0) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10240 ? 1 : 0)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

function ago(seconds) {
  const n = Number(seconds)
  if (!Number.isFinite(n) || n <= 0) return ''
  const diff = Math.max(0, Date.now() / 1000 - n)
  if (diff < 60) return 'just now'
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)}d ago`
  return new Date(n * 1000).toLocaleDateString()
}

const GROUP_ORDER = ['Walkthroughs', 'Docs', 'Plans', 'Code', 'Data', 'Media', 'Other']

function groupRank(name) {
  const index = GROUP_ORDER.indexOf(name)
  return index === -1 ? GROUP_ORDER.length : index
}

/** Split the backend payload into ordered, filterable groups. */
function useGroups(index, filter) {
  return useMemo(() => {
    const artifacts = (index && index.artifacts) || []
    const needle = String(filter || '').trim().toLowerCase()
    const kept = needle
      ? artifacts.filter(item => {
          const haystack = `${item.label || ''} ${item.path || ''} ${item.kind || ''}`.toLowerCase()
          return haystack.includes(needle)
        })
      : artifacts

    const byGroup = new Map()
    for (const item of kept) {
      const group = item.group || 'Other'
      if (!byGroup.has(group)) byGroup.set(group, [])
      byGroup.get(group).push(item)
    }
    return Array.from(byGroup.entries())
      .sort((a, b) => groupRank(a[0]) - groupRank(b[0]) || a[0].localeCompare(b[0]))
      .map(([name, items]) => ({ name, items }))
  }, [index, filter])
}

// ── session search ─────────────────────────────────────────────────────────
// Searches across the sessions the focused profile has touched. The backend's
// `/sessions` route returns {sessions: [...]}, so the query stays cheap and
// live: the atom is updated on every `/index` poll.
function useSessionSearch(ctx, profile) {
  const [query, setQuery] = useState('')
  const [sessions, setSessions] = useState([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    const refresh = async () => {
      try {
        const data = await rest(ctx, '/sessions')
        if (!alive) return
        setSessions(Array.isArray(data) ? data : [])
      } catch {
        if (!alive) return
        // At minimum, live with the profile's own index as a stopgap until the
        // real endpoint is mounted.
        try {
          const d = await rest(ctx, '/index')
          setSessions(d && Array.isArray(d.sessions) ? d.sessions : [])
        } catch {
          setSessions([])
        }
      } finally {
        if (alive) setLoading(false)
      }
    }
    refresh()
    return () => { alive = false }
  }, [ctx])

  return { query, setQuery, sessions, loading }
}

// ── local markdown renderer (fallback for Streamdown) ───────────────────────
/**
 * A small, total markdown renderer producing React elements — no HTML strings,
 * no dangerouslySetInnerHTML. Covers what session docs actually use: headings,
 * paragraphs, fenced code, lists, tables, quotes, rules, and inline emphasis.
 * Typography inherits the app font (the profile sets Lexend) because nothing
 * here sets font-family.
 */
function inline(text, keyPrefix) {
  const nodes = []
  const pattern =
    /(`[^`]+`)|(\*\*[^*]+\*\*)|(__[^_]+__)|(\*[^*]+\*)|(_[^_]+_)|(\[[^\]]+\]\([^)\s]+\))/g
  let last = 0
  let match
  let index = 0

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) {
      nodes.push(text.slice(last, match.index))
    }
    const token = match[0]
    const key = `${keyPrefix}-i${index++}`

    if (token.startsWith('`')) {
      nodes.push(
        jsx(
          'code',
          {
            key,
            style: {
              fontFamily: 'var(--ui-font-mono, ui-monospace, monospace)',
              fontSize: '0.86em',
              padding: '0.1em 0.35em',
              borderRadius: 4,
              background: 'var(--ui-surface-secondary, rgba(127,127,127,0.14))',
            },
            children: token.slice(1, -1),
          },
          key,
        ),
      )
    } else if (token.startsWith('**') || token.startsWith('__')) {
      nodes.push(jsx('strong', { key, children: token.slice(2, -2) }, key))
    } else if (token.startsWith('[')) {
      const split = token.indexOf('](')
      const label = token.slice(1, split)
      const href = token.slice(split + 2, -1)
      nodes.push(
        jsx(
          'a',
          {
            key,
            href,
            target: '_blank',
            rel: 'noreferrer',
            style: { color: 'var(--ui-accent, #4c8dff)', textDecoration: 'underline' },
            children: label,
          },
          key,
        ),
      )
    } else {
      nodes.push(jsx('em', { key, children: token.slice(1, -1) }, key))
    }
    last = match.index + token.length
  }
  if (last < text.length) {
    nodes.push(text.slice(last))
  }
  return nodes
}

function MarkdownFallback({ text }) {
  const blocks = useMemo(() => {
    const lines = String(text || '').split(/\r?\n/)
    const out = []
    let i = 0
    let key = 0

    const push = node => {
      out.push(jsx(node.type, { ...node.props, key: `b${key++}` }))
    }

    while (i < lines.length) {
      const line = lines[i]

      // fenced code
      const fence = /^\s*(```|~~~)\s*([\w+#.-]*)\s*$/.exec(line)
      if (fence) {
        const lang = fence[2] || ''
        const body = []
        i += 1
        while (i < lines.length && !/^\s*(```|~~~)\s*$/.test(lines[i])) {
          body.push(lines[i])
          i += 1
        }
        i += 1
        push({
          type: 'pre',
          props: {
            style: {
              margin: '0.7em 0',
              padding: '0.7em 0.85em',
              overflowX: 'auto',
              borderRadius: 8,
              border: '1px solid var(--ui-stroke-secondary, rgba(127,127,127,0.25))',
              background: 'var(--ui-surface-secondary, rgba(127,127,127,0.10))',
              fontSize: '0.8rem',
              lineHeight: 1.5,
              fontFamily: 'var(--ui-font-mono, ui-monospace, monospace)',
            },
            children: [
              lang
                ? jsx('div', {
                    style: {
                      fontSize: '0.65rem',
                      letterSpacing: '0.06em',
                      textTransform: 'uppercase',
                      opacity: 0.6,
                      marginBottom: 6,
                    },
                    children: lang,
                  })
                : null,
              jsx('code', { children: body.join('\n') }),
            ],
          },
        })
        continue
      }

      // table
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
        const head = line.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim())
        i += 2
        const rows = []
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
          rows.push(lines[i].trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim()))
          i += 1
        }
        const cell = { border: '1px solid var(--ui-stroke-secondary, rgba(127,127,127,0.25))', padding: '4px 8px', textAlign: 'left' }
        push({
          type: 'table',
          props: {
            style: {
          borderCollapse: 'collapse',
          margin: '0.7em 0',
          fontSize: '0.82rem',
          // Wide tables scroll inside the pane instead of being cut off at the edge.
          width: '100%',
          maxWidth: '100%',
          tableLayout: 'auto',
          display: 'block',
          overflowX: 'auto',
          wordBreak: 'break-word',
        },
            children: [
              jsx('thead', {
                children: jsx('tr', {
                  children: head.map((h, hi) => jsx('th', { style: { ...cell, fontWeight: 600 }, children: inline(h, `th${hi}`) }, hi)),
                }),
              }),
              jsx('tbody', {
                children: rows.map((row, ri) =>
                  jsx('tr', { children: row.map((c, ci) => jsx('td', { style: cell, children: inline(c, `td${ri}-${ci}`) }, ci)) }, ri),
                ),
              }),
            ],
          },
        })
        continue
      }

      // heading
      const heading = /^(#{1,6})\s+(.*)$/.exec(line)
      if (heading) {
        const level = heading[1].length
        const sizes = ['1.35rem', '1.18rem', '1.05rem', '0.97rem', '0.92rem', '0.88rem']
        push({
          type: `h${level}`,
          props: {
            style: {
              margin: level <= 2 ? '1.1em 0 0.5em' : '0.9em 0 0.4em',
              fontSize: sizes[level - 1],
              fontWeight: 650,
              lineHeight: 1.3,
              // Long tokens (URLs, variable names) must never run off the right
              // edge. Otherwise every heading auto-collapses until the pane
              // resizes.
              overflowWrap: 'anywhere',
              wordBreak: 'break-word',
              minWidth: 0,
              maxWidth: '100%',
            },
            children: inline(heading[2], `h${level}`),
          },
        })
        i += 1
        continue
      }

      // horizontal rule
      if (/^\s*([-*_])\1{2,}\s*$/.test(line)) {
        push({
          type: 'hr',
          props: {
            style: {
              border: 0,
              borderTop: '1px solid var(--ui-stroke-secondary, rgba(127,127,127,0.25))',
              margin: '1.1em 0',
            },
          },
        })
        i += 1
        continue
      }

      // blockquote
      if (/^\s*>\s?/.test(line)) {
        const body = []
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
          body.push(lines[i].replace(/^\s*>\s?/, ''))
          i += 1
        }
        push({
          type: 'blockquote',
          props: {
            style: {
              margin: '0.7em 0',
              padding: '0.15em 0 0.15em 0.85em',
              borderLeft: '3px solid var(--ui-stroke-tertiary, rgba(127,127,127,0.4))',
              opacity: 0.9,
              // Long tokens inside a quote must wrap, not run off the edge.
              overflowWrap: 'anywhere',
              wordBreak: 'break-word',
              minWidth: 0,
              maxWidth: '100%',
            },
            children: inline(body.join(' '), 'bq'),
          },
        })
        continue
      }

      // lists
      const bullet = /^\s*[-*+]\s+(.*)$/.exec(line)
      const ordered = /^\s*\d+[.)]\s+(.*)$/.exec(line)
      if (bullet || ordered) {
        const isOrdered = Boolean(ordered)
        const items = []
        while (i < lines.length) {
          const b = /^\s*[-*+]\s+(.*)$/.exec(lines[i])
          const o = /^\s*\d+[.)]\s+(.*)$/.exec(lines[i])
          if (isOrdered && o) {
            items.push(o[1])
          } else if (!isOrdered && b) {
            items.push(b[1])
          } else if (/^\s{2,}\S/.test(lines[i]) && items.length) {
            // lazy continuation line
            items[items.length - 1] += ` ${lines[i].trim()}`
          } else {
            break
          }
          i += 1
        }
        push({
          type: isOrdered ? 'ol' : 'ul',
          props: {
            style: {
              margin: '0.6em 0',
              paddingLeft: '1.4em',
              maxWidth: '100%',
              overflowWrap: 'anywhere',
              wordBreak: 'break-word',
            },
            children: items.map((item, ii) =>
              jsx('li', {
                style: {
                  margin: '0.22em 0',
                  overflowWrap: 'anywhere',
                  wordBreak: 'break-word',
                  minWidth: 0,
                },
                children: inline(item, `li${ii}`),
              }, ii),
            ),
          },
        })
        continue
      }

      // blank
      if (!line.trim()) {
        i += 1
        continue
      }

      // paragraph
      const para = [line]
      i += 1
      while (
        i < lines.length &&
        lines[i].trim() &&
        !/^\s*(#{1,6}\s|[-*+]\s|\d+[.)]\s|>|```|~~~|\|)/.test(lines[i]) &&
        !/^\s*([-*_])\1{2,}\s*$/.test(lines[i])
      ) {
        para.push(lines[i])
        i += 1
      }
      push({
        type: 'p',
        props: {
          style: {
            margin: '0.6em 0',
            lineHeight: 1.62,
            // Without these a long path/URL runs into the pane edge and is clipped.
            overflowWrap: 'anywhere',
            wordBreak: 'break-word',
            minWidth: 0,
            maxWidth: '100%',
          },
          children: inline(para.join(' '), 'p'),
        },
      })
    }

    return out
  }, [text])

  return jsx('div', {
        style: {
          fontSize: '0.86rem',
          // Adaptive: never let the fallback document exceed the pane. Without
          // these the fallback ran into the right edge and was clipped.
          maxWidth: '100%',
          minWidth: 0,
          overflowWrap: 'anywhere',
          wordBreak: 'break-word',
          overflowX: 'auto',
        },
        children: blocks,
      });
  }

/** Keeps a third-party renderer from taking the whole pane down with it. */
class Boundary extends Component {
  constructor(props) {
    super(props)
    this.state = { failed: false }
  }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  componentDidCatch() {
    /* the fallback IS the report */
  }

  componentDidUpdate(prev) {
    if (prev.resetKey !== this.props.resetKey && this.state.failed) {
      this.setState({ failed: false })
    }
  }

  render() {
    if (this.state.failed) {
      return this.props.fallback
    }
    return this.props.children
  }
}

function MarkdownView({ text }) {
  const clipped = text.length > MAX_TEXT_RENDER ? `${text.slice(0, MAX_TEXT_RENDER)}\n\n… truncated for display` : text
  const fallback = jsx(MarkdownFallback, { text: clipped })

  if (!Streamdown) {
    return fallback
  }
  return jsx(
    Boundary,
    {
      resetKey: clipped.length,
      fallback,
      children: jsx(
        'div',
        {
          className: 'athena-markdown',
          style: {
            fontSize: '0.86rem',
            // Adaptive: never let the rendered document exceed the pane. Without
            // these the markdown ran into the right edge and was clipped.
            maxWidth: '100%',
            minWidth: 0,
            overflowWrap: 'anywhere',
            wordBreak: 'break-word',
            overflowX: 'auto',
          },
          children: jsx(Streamdown, { children: clipped }),
        },
      ),
    },
  )
}

// ── viewers ─────────────────────────────────────────────────────────────────

/** Per-status glyph + colour for one task row. */
const TASK_STATUS = {
  completed: { glyph: '\u2713', color: 'var(--ui-success, #3fb950)' },
  in_progress: { glyph: '\u25b6', color: 'var(--ui-accent, #4c8dff)' },
  blocked: { glyph: '\u26a0', color: 'var(--ui-warning, #d29922)' },
  pending: { glyph: '\u25cb', color: 'var(--ui-text-tertiary, #8b949e)' },
}

/**
 * A synthesized tasklist, rendered as a checklist.
 *
 * The data is virtual: `/file?path=plan://…` returns the LATEST tasklist for the
 * session as JSON. Because the route is polled on the same cadence as `/index`,
 * ticking a task off in the composer flips its row here without a refresh.
 */
function TasklistView({ ctx, session, artifact }) {
  const path = (artifact && artifact.path) || ''
  const query = useRest(
    ctx,
    path ? `/file?session=${encodeURIComponent(session || '')}&path=${encodeURIComponent(path)}` : '',
    { enabled: Boolean(path), interval: POLL_MS },
  )

  if (query.loading && !query.data) {
    return jsx(Empty, { title: 'Loading tasklist…', body: 'Reading the latest tasklist for this session.' })
  }
  const payload = query.data
  if (query.error || !payload || payload.ok === false) {
    return jsx(Empty, {
      title: 'No tasklist yet',
      body: (payload && payload.error) || query.error || 'This session has not written a tasklist.',
      extra: 'Call the plan / todo tool and Athena will show it here, live.',
    })
  }

  const tasks = Array.isArray(payload.tasks) ? payload.tasks : []
  const total = tasks.length
  const done = tasks.filter(t => t && t.status === 'completed').length
  const pct = total ? Math.round((done / total) * 100) : 0

  const rows = tasks.map((task, i) => {
    const status = (task && task.status) || 'pending'
    const meta = TASK_STATUS[status] || TASK_STATUS.pending
    const isDone = status === 'completed'
    return jsxs(
      'div',
      {
        key: `${(task && task.id) || i}`,
        style: {
          display: 'flex',
          alignItems: 'flex-start',
          gap: 8,
          padding: '6px 2px',
          borderBottom: '1px solid var(--ui-stroke-secondary, rgba(127,127,127,0.12))',
        },
        children: [
          jsx('span', {
            style: { color: meta.color, fontSize: '0.8rem', lineHeight: '1.35rem', flex: '0 0 auto' },
            children: meta.glyph,
          }),
          jsx('span', {
            style: {
              fontSize: '0.8rem',
              lineHeight: '1.35rem',
              flex: '1 1 auto',
              minWidth: 0,
              color: isDone ? 'var(--ui-text-tertiary, #8b949e)' : 'inherit',
              textDecoration: isDone ? 'line-through' : 'none',
              opacity: isDone ? 0.75 : 1,
            },
            children: String((task && task.label) || ''),
          }),
        ],
      },
    )
  })

  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', minHeight: 0, height: '100%' },
    children: [
      jsxs('div', {
        style: { padding: '10px 12px 8px', borderBottom: '1px solid var(--ui-stroke-secondary, rgba(127,127,127,0.18))' },
        children: [
          jsx('div', {
            style: { fontSize: '0.82rem', fontWeight: 600, marginBottom: 6 },
            children: payload.label || artifact.label,
          }),
          jsxs('div', {
            style: { display: 'flex', alignItems: 'center', gap: 8 },
            children: [
              jsxs('div', {
                style: { flex: '1 1 auto', height: 4, borderRadius: 2, background: 'var(--ui-surface-secondary, rgba(127,127,127,0.18))', overflow: 'hidden' },
                children: [
                  jsx('div', {
                    style: { width: `${pct}%`, height: '100%', background: 'var(--ui-success, #3fb950)', transition: 'width 0.25s ease' },
                  }),
                ],
              }),
              jsx('span', {
                style: { fontSize: '0.68rem', opacity: 0.7, flex: '0 0 auto' },
                children: `${done}/${total}`,
              }),
            ],
          }),
        ],
      }),
      jsx('div', {
        style: { flex: '1 1 auto', overflow: 'auto', padding: '0 12px 16px' },
        children: total
          ? jsx('div', { children: rows })
          : jsx(Empty, { title: 'Empty tasklist', body: 'No tasks recorded for this session yet.' }),
      }),
    ],
  })
}

function FileView({ ctx, session, artifact, live = false }) {
  const path = artifact && artifact.path
  // `live` re-polls the body so a plan / walkthrough that Hermes keeps editing
  // visibly updates in the open tab instead of freezing at first read.
  const query = useRest(
    ctx,
    path ? `/file?session=${encodeURIComponent(session || '')}&path=${encodeURIComponent(path)}` : '',
    { enabled: Boolean(path), interval: live ? POLL_MS : 0 },
  )
  const [dataUrl, setDataUrl] = useState('')

  const isImage = artifact && artifact.kind === 'image'
  const isHtml = artifact && artifact.kind === 'html'

  // Images and HTML previews render from bytes via the preload bridge; the text
  // route cannot represent them.
  useEffect(() => {
    let live = true
    setDataUrl('')
    if (!path || (!isImage && !isHtml)) {
      return undefined
    }
    const bridge = typeof window === 'undefined' ? null : window.hermesDesktop
    if (!bridge || typeof bridge.readFileDataUrl !== 'function') {
      return undefined
    }
    try {
      Promise.resolve(bridge.readFileDataUrl(path))
        .then(value => {
          if (live && typeof value === 'string' && value) setDataUrl(value)
        })
        .catch(() => {})
    } catch {
      /* leave the viewer in its message state */
    }
    return () => {
      live = false
    }
  }, [path, isImage, isHtml])

  if (!artifact) {
    return jsx(Empty, { title: 'Nothing open', body: 'Pick a file on the left to read it here.' })
  }
  // A tasklist has no file behind it. `/file?path=plan://…` returns the
  // synthesized checklist as JSON, so render it as a LIVE checklist that
  // re-polls with the rest of the pane — not as a dead placeholder.
  if (artifact.kind === 'plan') {
    return jsx(TasklistView, { ctx, session, artifact })
  }
  if (artifact.pending && !artifact.exists) {
    return jsx(Empty, { title: 'Not created yet', body: `${artifact.label} was referenced by ${artifact.tool || 'a tool'} but is not on disk.` })
  }
  if (artifact.denied) {
    return jsx(Empty, { title: 'Refused', body: 'This path looks like a credential file, so Athena will not read it.' })
  }

  if (isImage || isHtml) {
    if (!dataUrl) {
      return jsx(Empty, { title: 'Loading…', body: `Reading ${artifact.label} from disk.` })
    }
    if (isHtml && SandboxedFrame) {
      return jsxs('div', {
        style: { display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 },
        children: [
          jsx(SandboxedFrame, {
            src: dataUrl,
            title: `Preview of ${artifact.label}`,
            sandbox: 'allow-scripts',
            style: { flex: '1 1 auto', width: '100%', border: 0, background: '#fff' },
          }),
          jsx(OpenOutside, { ctx, artifact }),
        ],
      })
    }
    if (isImage) {
      return jsxs('div', {
        style: { display: 'flex', flexDirection: 'column', gap: 8, padding: 10, overflow: 'auto' },
        children: [
          jsx('img', {
            src: dataUrl,
            alt: artifact.label,
            style: { maxWidth: '100%', borderRadius: 8, border: '1px solid var(--ui-stroke-secondary, rgba(127,127,127,0.25))' },
          }),
          jsx(OpenOutside, { ctx, artifact }),
        ],
      })
    }
    return jsx(Empty, { title: 'Preview unavailable', body: 'Open this file outside the app to view it.' })
  }

  if (query.loading && !query.data) {
    return jsx(Empty, { title: 'Loading…', body: `Reading ${path}` })
  }
  const payload = query.data
  if (query.error || !payload || payload.ok === false) {
    return jsx(Empty, {
      title: 'Could not read this file',
      body: (payload && payload.error) || query.error || 'Unknown error.',
      extra: path,
    })
  }
  if (payload.binary) {
    return jsx(Empty, { title: 'Binary file', body: `${artifact.label} is not text, so there is nothing to render in-panel.`, extra: path })
  }

  const text = payload.text || ''
  const isCode = artifact.kind === 'code' || artifact.kind === 'data'
  if (artifact.kind === 'markdown') {
    return jsxs('div', {
      style: { display: 'flex', flexDirection: 'column', minHeight: 0, height: '100%' },
      children: [
        jsx('div', {
          style: {
            flex: '1 1 auto',
            overflow: 'auto',
            padding: '0 12px 16px',
            // Padding does not increase a fixed-height box: the scroll area is the
            // content box alone. boxSizing removes that mismatch so nothing is
            // clipped and the margin is actually visible.
            boxSizing: 'border-box',
          },
          children: jsx(MarkdownView, { text }),
        }),
        payload.truncated ? jsx(Truncated, {}) : null,
      ],
    })
  }

  if (isCode) {
    const lines = text.split(/\r?\n/)
    return jsxs('div', {
      style: { display: 'flex', flexDirection: 'column', minHeight: 0, height: '100%' },
      children: [
        jsx('div', {
          style: {
            flex: '1 1 auto',
            overflow: 'auto',
            fontFamily: 'var(--ui-font-mono, ui-monospace, monospace)',
            fontSize: '0.78rem',
            lineHeight: 1.55,
          },
          children: jsx('table', {
            style: { borderCollapse: 'collapse', width: '100%' },
            children: jsx('tbody', {
              children: lines.map((line, index) =>
                jsxs(
                  'tr',
                  {
                    children: [
                      jsx('td', {
                        style: {
                          width: 1,
                          padding: '0 8px 0 10px',
                          textAlign: 'right',
                          opacity: 0.4,
                          userSelect: 'none',
                          verticalAlign: 'top',
                          whiteSpace: 'nowrap',
                        },
                        children: String(index + 1),
                      }),
                      jsx('td', {
                        style: { padding: '0 10px 0 0', whiteSpace: 'pre-wrap', wordBreak: 'break-word' },
                        children: line || ' ',
                      }),
                    ],
                  },
                  index,
                ),
              ),
            }),
          }),
        }),
        payload.truncated ? jsx(Truncated, {}) : null,
      ],
    })
  }

  // html/data/other as plain text
  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', minHeight: 0, height: '100%' },
    children: [
      jsx('pre', {
        style: {
          flex: '1 1 auto',
          overflow: 'auto',
          margin: 0,
          padding: '10px 12px 24px',
          fontFamily: 'var(--ui-font-mono, ui-monospace, monospace)',
          fontSize: '0.78rem',
          lineHeight: 1.55,
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
        },
        children: text,
      }),
      payload.truncated ? jsx(Truncated, {}) : null,
    ],
  })
}

function Truncated() {
  return jsx('div', {
    style: {
      flex: '0 0 auto',
      padding: '6px 12px',
      fontSize: '0.68rem',
      opacity: 0.75,
      borderTop: '1px solid var(--ui-stroke-secondary, rgba(127,127,127,0.25))',
    },
    children: 'Truncated for display — open the file on disk for the full contents.',
  })
}

function OpenOutside({ ctx, artifact }) {
  return jsx('button', {
    type: 'button',
    onClick: () => {
      try {
        if (ctx && ctx.os && typeof ctx.os.openExternal === 'function') {
          void Promise.resolve(ctx.os.openExternal(artifact.path)).catch(() => {})
        }
      } catch {
        /* opening outside is a convenience, not a guarantee */
      }
    },
    style: {
      flex: '0 0 auto',
      alignSelf: 'flex-start',
      margin: '6px 10px',
      padding: '4px 9px',
      fontSize: '0.72rem',
      borderRadius: 6,
      cursor: 'pointer',
      color: 'var(--ui-text-secondary, inherit)',
      background: 'transparent',
      border: '1px solid var(--ui-stroke-secondary, rgba(127,127,127,0.3))',
    },
    children: 'Open outside the app',
  })
}

function Empty({ title, body, extra }) {
  return jsxs('div', {
    style: {
      display: 'flex',
      flexDirection: 'column',
      gap: 6,
      padding: '18px 16px',
      fontSize: '0.75rem',
      opacity: 0.85,
    },
    children: [
      jsx('div', { style: { fontWeight: 600, fontSize: '0.8rem' }, children: title }),
      body ? jsx('div', { children: body }) : null,
      extra
        ? jsx('div', {
            style: {
              fontFamily: 'var(--ui-font-mono, ui-monospace, monospace)',
              fontSize: '0.68rem',
              opacity: 0.7,
              wordBreak: 'break-all',
            },
            children: extra,
          })
        : null,
    ],
  })
}

// ── list ────────────────────────────────────────────────────────────────────
function ArtifactRow({ artifact, active, onOpen }) {
  const meta = [ago(artifact.created_at), humanSize(artifact.size_bytes), artifact.tool].filter(Boolean).join(' \u00b7 ')
  return jsxs('button', {
    type: 'button',
    onClick: () => onOpen(artifact),
    title: artifact.path,
    style: {
      display: 'flex',
      alignItems: 'flex-start',
      gap: 7,
      width: '100%',
      textAlign: 'left',
      padding: '5px 7px',
      borderRadius: 6,
      border: '1px solid transparent',
      cursor: 'pointer',
      background: active ? 'var(--chrome-action-hover, rgba(127,127,127,0.14))' : 'transparent',
      color: 'inherit',
      opacity: artifact.readable || artifact.kind === 'plan' ? 1 : 0.55,
    },
    children: [
      jsx('span', {
        style: { flex: '0 0 auto', fontSize: '0.78rem', lineHeight: 1.25 },
        children: artifact.icon || '\u2022',
      }),
      jsxs('span', {
        style: { display: 'flex', flexDirection: 'column', minWidth: 0, flex: '1 1 auto' },
        children: [
          jsx('span', {
            style: { fontSize: '0.75rem', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' },
            children: artifact.label,
          }),
          meta
            ? jsx('span', {
                style: { fontSize: '0.65rem', opacity: 0.6, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' },
                children: meta,
              })
            : null,
        ],
      }),
    ],
  })
}

// ── the view ────────────────────────────────────────────────────────────────
function AthenaView({ ctx, variant }) {
  // Measure the rendered pane: the split view needs real width, and a CSS
  // media query cannot see a plugin's own container.
  const rootRef = useRef(null)
  const [wide, setWide] = useState(Boolean(variant === 'page'))
  useEffect(() => {
    const node = rootRef && rootRef.current
    if (!node || typeof ResizeObserver === 'undefined') return undefined
    const measure = () => setWide(node.clientWidth >= 760)
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(node)
    return () => ro.disconnect()
  }, [])
  const runtimeId = useAtomValue(focusedSessionAtom)
  const storedId = useAtomValue(focusedStoredSessionAtom)
  const profile = useAtomValue(focusedProfileAtom)
  // Prefer the STORED id. `focusedSessionId` is a runtime handle (a session
  // tile's runtimeId, else the active runtime id) and `state.db` is keyed by the
  // durable stored id, so querying with the runtime id matches no messages and
  // the pane silently reports 0 files. On a fresh reload the two can agree,
  // which is why this looked intermittent: it only breaks once the renderer has
  // adopted a live runtime id for the focused tile.
  const session = storedId || runtimeId || ''

  const [filter, setFilter] = useState('')
  const [tabs, setTabs] = useState([])
  const [active, setActive] = useState('')
  const [showAll, setShowAll] = useState(false)  // all-sessions mode (left-side full tab)
  const [querySession, setQuerySession] = useState('');  // when in all-sessions mode, the session whose artifacts to show

  // showAll documents the full all-sessions view. When on, the list swaps from

  // the focused session's artifacts to every session the focused profile has

  // touched (the /sessions feed). The same switch moves the search bar into

  // all-sessions mode; the two stay in sync.

  // ── session search ────────────────────────────────────────────────────────
  // In Athena's default the list is one session. The user may widen this: the
  // `profile` prop carries the focused profile, and `/sessions` serves every
  // session the profile has touched, so the search bar filters the whole set.
  // The backend returns a lightweight {sessions:[...]} toast; the full index
  // stays per-session.
  const { query: seshQuery, setQuery: setSeshQuery, sessions, loading: seshLoading } = useSessionSearch(ctx, profile)

    // The workspace scan is ALWAYS on. It used to be a header toggle, but leaving it
    // off meant artifacts written outside a tool call (a scaffold, a script's own
    // output) never appeared, and users did not know to reach for the button. The
    // backend bounds the walk by depth, entry count and age, so it is cheap enough
    // to run on every poll.
    const query = useLiveSessionIndex(ctx, session, { scan: true, profile })

  const index = query.data || null
  const groups = useGroups(index, filter)
  const artifacts = (index && index.artifacts) || []
  const byPath = useMemo(() => {
    const map = new Map()
    for (const item of artifacts) map.set(item.path, item)
    return map
  }, [artifacts])

  // ── consume "open this file in Athena" requests ────────────────────────────
  // The requester (a transcript file card, a ::athena chip) lives in a
  // different React tree, so it hands the path over through the module-level
  // mailbox. A request can arrive BEFORE the index has loaded, so it is held in
  // a ref and re-applied once the artifact actually appears.
  const pendingOpen = useRef('')
  const lastToken = useRef(0)

  const applyOpen = useCallback(
    wanted => {
      if (!wanted) return
      // Exact match first, then case-insensitive (Windows), then basename —
      // the transcript may carry a slightly different spelling of the path.
      let target =
        byPath.get(wanted) ||
        artifacts.find(item => String(item.path || '').toLowerCase() === wanted.toLowerCase()) ||
        artifacts.find(item => {
          const name = String(item.label || '')
          const tail = wanted.replace(/\\/g, '/').split('/').pop() || ''
          return tail && name.toLowerCase() === tail.toLowerCase()
        })
      if (!target) {
        pendingOpen.current = wanted
        return
      }
      pendingOpen.current = ''
      setTabs(prev => (prev.includes(target.path) ? prev : [...prev, target.path]))
      setActive(target.path)
    },
    [artifacts, byPath],
  )

  useEffect(() => subscribeOpen(next => {
    if (!next || next.token === lastToken.current) return
    lastToken.current = next.token
    applyOpen(next.path)
  }), [applyOpen])

  // A request made while this pane was NOT mounted (user closed the zone, or the
  // pane was never adopted) would otherwise be dropped on the floor — there is
  // no subscriber to notify. peekOpen() on mount picks it up. Skipping the token
  // guard here is deliberate: lastToken is 0 on a fresh mount, and the mailbox
  // only holds a request that is still unconsumed.
  useEffect(() => {
    const waiting = peekOpen()
    if (waiting && waiting.token !== lastToken.current) {
      lastToken.current = waiting.token
      applyOpen(waiting.path)
    }
  }, [applyOpen])

  // Re-apply a request that arrived before the index knew the artifact.
  useEffect(() => {
    if (!pendingOpen.current || !artifacts.length) return
    applyOpen(pendingOpen.current)
  }, [artifacts, applyOpen])

  // Drop tabs whose artifact vanished from the index; keep the rest.
  useEffect(() => {
    if (!index) return
    setTabs(prev => prev.filter(path => byPath.has(path)))
  }, [index, byPath])

  // Reset the open set when the focused session changes.
  const sessionKey = session || ''
  const lastSession = useRef(sessionKey)
  useEffect(() => {
    if (lastSession.current !== sessionKey) {
      lastSession.current = sessionKey
      setTabs([])
      setActive('')
    }
  }, [sessionKey])

// ── auto-open REMOVED on purpose ──────────────────────────────────────────
  // Two effects used to seize the pane:
  //   1. it staged the newest readable artifact whenever nothing was active, so
  //      after Back returned to the list it immediately reopened a file — the
  //      pane could never rest on the list;
  //   2. it opened every plan / walkthrough / tasklist the moment it appeared in
  //      the polled index — up to 12 tabs at once, and `selftest.py` matched via
  //      the /plan/ test on its filename.
  // A file now opens ONLY on a click, or on an explicit "Open in Athena" request
  // (a file-card action, or a ::athena directive). The list is a real resting
  // state; surfacing new work belongs in the LIST as a highlight, not as an
  // already-opened tab.

  // ── Back button history ──────────────────────────────────────────────────
  // A flat LIFO stack of visited tabs: every navigation pushes the file being
  // left, Back pops one. Entries are dropped when a tab is closed so Back can
  // never land on a tab that no longer exists.
  const [backStack, setBackStack] = useState([])

  const openArtifact = useCallback(artifact => {
    if (!artifact || !artifact.path) return
    setTabs(prev => (prev.includes(artifact.path) ? prev : [...prev, artifact.path]))
    setActive(current => {
      // Push the outgoing file so Back can return to it.
      if (current && current !== artifact.path) setBackStack(prevStack => [...prevStack, current])
      return artifact.path
    })
  }, [])

  const closeTab = useCallback(path => {
    setTabs(prev => {
      const next = prev.filter(item => item !== path)
      setActive(current => (current === path ? (next.length ? next[next.length - 1] : '') : current))
      return next
    })
    setBackStack(prev => prev.filter(item => item !== path))
  }, [])

  // Route every tab switch through here so the history cannot be bypassed.
  const openTab = useCallback(path => {
    setActive(current => {
      if (current && current !== path) setBackStack(prev => [...prev, current])
      return path
    })
  }, [])

  const goBack = useCallback(() => {
    setBackStack(prev => {
      let stack = prev
      while (stack.length) {
        const target = stack[stack.length - 1]
        if (tabs.includes(target)) {
          setActive(target)
          return stack.slice(0, -1)
        }
        stack = stack.slice(0, -1) // tab was closed; skip it
      }
      // History exhausted. In a narrow pane the list is hidden while a file is
      // open, so clearing the active tab is what brings the list back.
      setActive('')
      return []
    })
  }, [tabs])

  const counts = (index && index.counts) || {}
  const diagnostics = (index && index.diagnostics) || {}
  const activeArtifact = active ? byPath.get(active) : null

  const header = jsxs('div', {
    style: { display: 'flex', alignItems: 'center', gap: 6, padding: '8px 10px 6px' },
    children: [
      jsx(Icon, { name: 'book', size: '0.9rem' }),
      jsxs('span', {
        style: { display: 'flex', flexDirection: 'column', minWidth: 0, flex: '1 1 auto' },
        children: [
          jsx('span', { style: { fontSize: '0.78rem', fontWeight: 600 }, children: 'Athena' }),
          jsx('span', {
            style: { fontSize: '0.65rem', opacity: 0.6, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' },
            title: session,
            children: session
              ? `${counts.total || 0} file${counts.total === 1 ? '' : 's'}${index && index.cwd ? ` \u00b7 ${String(index.cwd).split(/[\\/]/).filter(Boolean).pop()}` : ''}`
              : 'No session focused',
          }),
        ],
      }),
      // Back lives in the HEADER, not in the tab strip: in a narrow pane the
      // strip sits inside the viewer's flex column and got squeezed to zero
      // height, so a Back placed there was invisible exactly when it was needed.
      // The header is always rendered, so Back is always reachable.
      // Hidden when no file is open. Uses `active` (not `tabs.length`) so it
      // matches the layout: Back only ever clears `active`.
      active
        ? jsx(HeaderButton, {
            label: backStack.length ? 'Back' : 'Back to the file list',
            showLabel: 'Back',
            icon: 'arrow-left',
            active: false,
            onClick: goBack,
          })
        : null,
      jsx(HeaderButton, {
        label: showAll ? 'Narrow to this session' : 'All sessions',
        icon: showAll ? 'grid-view' : 'grid-view',
        onClick: () => setShowAll(v => !v),
        active: showAll,
      }),
      jsx(HeaderButton, {
        label: 'Refresh',
        icon: 'refresh',
        onClick: () => query.refresh(),
      }),
      // ── session search bar ─────────────────────────────────────────────────
      // Filters the sessions Athena lists. The focused session is the default
      // panel, but the same bar also drives the "all sessions" mode (the
      // showAll state above): when set, the panel reads /sessions and lists
      // every session the focused profile has touched, with a live count.
      showAll
        ? jsx('input', {
            value: seshQuery,
            onChange: e => setSeshQuery(e.target.value),
            placeholder: 'Search all sessions…',
            style: {
              width: '100%',
              boxSizing: 'border-box',
              fontSize: '0.72rem',
              padding: '4px 8px',
              borderRadius: 6,
              color: 'inherit',
              background: 'var(--ui-surface-secondary, rgba(127,127,127,0.10))',
              border: '1px solid var(--ui-stroke-secondary, rgba(127,127,127,0.25))',
            },
          })
        : null,
    ],
  })

  const body = []
  body.push(
    jsx('div', {
      key: 'filter',
      style: { padding: '0 10px 6px' },
      children: jsx('input', {
        value: filter,
        onChange: event => setFilter(event.target.value),
        placeholder: 'Filter files…',
        style: {
          width: '100%',
          boxSizing: 'border-box',
          fontSize: '0.72rem',
          padding: '4px 8px',
          borderRadius: 6,
          color: 'inherit',
          background: 'var(--ui-surface-secondary, rgba(127,127,127,0.10))',
          border: '1px solid var(--ui-stroke-secondary, rgba(127,127,127,0.25))',
        },
      }),
    }),
  )

  const list = []
  if (showAll) {
    // "All sessions" full tab: every session the focused profile has touched.
    // The user types in the search bar above to filter this list; the bar is
    // tied to the same `seshQuery` state.
    if (seshLoading) {
      list.push(jsx(Empty, { key: 'sess-loading', title: 'Loading sessions…', body: 'Reading the session list from the backend.' }))
    } else if (seshQuery.trim()) {
      // Live filter: keep only rows whose title or id matches the query.
      const needle = seshQuery.trim().toLowerCase()
      const kept = (sessions || []).filter(
        s => !needle || `${s.title || ''} ${s.session_id || ''}`.toLowerCase().includes(needle),
      )
      if (!kept.length) {
        list.push(jsx(Empty, { key: 'sess-nomatch', title: 'No match', body: `Nothing matches "${seshQuery}".` }))
      } else {
        const rows = kept.map(s =>
          jsx('div', {
            key: s.session_id,
            style: {
              display: 'flex', alignItems: 'center', gap: 8,
              padding: '6px 10px', borderRadius: 6,
              background: 'var(--ui-surface-secondary, rgba(127,127,127,0.08))',
              border: '1px solid var(--ui-stroke-secondary, rgba(127,127,127,0.16))',
              cursor: 'pointer',
              wordBreak: 'break-all',
            },
            onClick: () => {
              setShowAll(false)
              setQuerySession(s.session_id)
              setTabs([])
              setActive('')
            },
            children: [
              jsx('span', { style: { flex: '0 0 auto', fontSize: '0.72rem', opacity: 0.6, fontWeight: 600 }, children: s.artifact_count ? String(s.artifact_count) : '—' }),
              jsx('span', { style: { flex: '1 1 auto', minWidth: 0, fontSize: '0.8rem', color: 'inherit', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, title: s.title, children: s.title }),
              jsx('span', { style: { flex: '0 0 auto', fontSize: '0.62rem', opacity: 0.45 }, children: new Date(s.started_at || 0).toLocaleDateString() }),
            ],
          })
        )
        list.push(jsx('div', { key: 'sess-list', style: { display: 'flex', flexDirection: 'column', gap: 3, paddingBottom: 6, overflowY: 'auto', flex: '1 1 auto', minWidth: 0 } }, rows))
      }
    } else {
      list.push(
        jsx('div', { key: 'sess-empty', style: { padding: '12px 10px' } },
          jsx('p', { style: { fontSize: '0.72rem', color: 'var(--ui-text-tertiary)', margin: 0, marginBottom: 8 } },
            'Every session this profile has touched, newest first. Type in the bar above to filter; tap a row to open its artifacts.'),
        ),
        jsx('div', { key: 'sess-list', style: { display: 'flex', flexDirection: 'column', gap: 3, paddingBottom: 6, overflowY: 'auto', flex: '1 1 auto', minWidth: 0 } }, (sessions || []).map(s =>
          jsx('div', {
            key: s.session_id,
            style: {
              display: 'flex', alignItems: 'center', gap: 8,
              padding: '6px 10px', borderRadius: 6,
              background: 'var(--ui-surface-secondary, rgba(127,127,127,0.08))',
              border: '1px solid var(--ui-stroke-secondary, rgba(127,127,127,0.16))',
              cursor: 'pointer',
              wordBreak: 'break-all',
            },
            onClick: () => {
              setShowAll(false)
              setQuerySession(s.session_id)
              setTabs([])
              setActive('')
            },
            children: [
              jsx('span', { style: { flex: '0 0 auto', fontSize: '0.72rem', opacity: 0.6, fontWeight: 600 }, children: s.artifact_count ? String(s.artifact_count) : '—' }),
              jsx('span', { style: { flex: '1 1 auto', minWidth: 0, fontSize: '0.8rem', color: 'inherit', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, title: s.title, children: s.title }),
              jsx('span', { style: { flex: '0 0 auto', fontSize: '0.62rem', opacity: 0.45 }, children: new Date(s.started_at || 0).toLocaleDateString() }),
            ],
          })
        )),
      )
    }
  } else if (!session) {
    list.push(jsx(Empty, { key: 'noses', title: 'No session focused', body: 'Open a session and Athena will list what it produced.' }))
  } else if (query.loading && !index) {
    list.push(jsx(Empty, { key: 'load', title: 'Loading…', body: 'Reading this session\u2019s artifacts.' }))
  } else if (!index || index.ok === false) {
    list.push(
      jsx(Empty, {
        key: 'err',
        title: 'Athena could not read the session store',
        body: query.error || 'The plugin backend did not answer. Open Diagnostics for details.',
        extra: Object.entries(diagnostics)
          .map(([key, value]) => `${key}: ${value}`)
          .join('\n'),
      }),
    )
  } else if (!artifacts.length) {
    list.push(
      jsx(Empty, {
        key: 'none',
        title: 'No artifacts yet',
        body:
          'This session has not written any files yet. Athena lists what this session produced — plans, walkthroughs, docs, code and media — so give it a moment, or ask for something to be made.',
        extra: diagnostics.cwd ? `workspace: ${index.cwd || 'unknown'}` : '',
      }),
    )
  } else if (!groups.length) {
    list.push(jsx(Empty, { key: 'nofilter', title: 'No match', body: `Nothing matches "${filter}".` }))
  } else {
    for (const group of groups) {
      list.push(
        jsxs('div', {
          key: group.name,
          style: { marginTop: 6 },
          children: [
            jsx('div', {
              style: {
                padding: '3px 10px',
                fontSize: '0.62rem',
                letterSpacing: '0.08em',
                textTransform: 'uppercase',
                opacity: 0.55,
                fontWeight: 600,
              },
              children: `${group.name} (${group.items.length})`,
            }),
            group.items.map(item =>
              jsx(ArtifactRow, {
                artifact: item,
                active: active === item.path,
                onOpen: openArtifact,
              }),
            ),
          ],
        }),
      )
    }
  }

  const isPage = variant === 'page'
  return jsxs('div', {
    ref: rootRef,
    style: {
      display: 'flex',
      flexDirection: 'column',
      height: '100%',
      minHeight: 0,
      overflow: 'hidden',
      textAlign: 'left',
      maxWidth: isPage ? 1100 : undefined,
      margin: isPage ? '0 auto' : undefined,
      width: '100%',
    },
    children: [
      header,
      // Narrow panes cannot host a list AND a document side by side: at ~420px the
      // viewer is left ~100px wide and the markdown is unreadable. So in a pane,
      // OPENING A FILE TAKES OVER THE WHOLE PANE — the list is hidden and Back
      // (in the tab strip) brings the list back. A wide pane, and the full page,
      // keep the split view.
      isPage || wide
        ? jsxs('div', {
            style: { display: 'flex', flex: '1 1 auto', minHeight: 0, gap: 10, padding: '0 10px 10px' },
            children: [
              jsx('div', {
                style: {
                  // Proportional, clamped: the list gets ~40% but never less than
                  // 180px nor more than 380px, so the reader always keeps the
                  // larger share no matter how the pane is dragged.
                  flex: '0 0 clamp(180px, 38%, 380px)',
                  minWidth: 0,
                  overflowY: 'auto',
                  overflowX: 'hidden',
                  borderRight: '1px solid var(--ui-stroke-secondary, rgba(127,127,127,0.25))',
                },
                children: list,
              }),
              jsx('div', {
                style: { flex: '1 1 auto', minWidth: 0, display: 'flex', flexDirection: 'column' },
                children: viewer(),
              }),
            ],
          })
        // Keyed on `active`, NOT `tabs.length`: Back clears `active` but leaves
        // the tabs open, so a `tabs.length` test kept rendering the viewer after
        // Back and the file list never came back.
        : active
          ? jsx('div', {
              style: { display: 'flex', flex: '1 1 auto', minHeight: 0, minWidth: 0 },
              children: viewer(),
            })
          : jsx('div', {
              style: { flex: '1 1 auto', minHeight: 0, overflowY: 'auto' },
              children: list,
            }),
    ],
  })

  function viewer() {
    if (!active || !tabs.length) {
      return jsx(Empty, { title: 'Nothing open', body: 'Choose a file from the list to read it here.' })
    }
    return jsxs('div', {
      style: { display: 'flex', flexDirection: 'column', flex: '1 1 auto', minHeight: 0 },
      children: [
        jsx('div', {
          style: {
            display: 'flex',
            gap: 4,
            overflowX: 'auto',
            padding: '4px 6px',
            borderBottom: '1px solid var(--ui-stroke-secondary, rgba(127,127,127,0.25))',
          },
          children: [
            ...tabs.map(path => {
            const item = byPath.get(path)
            const label = item ? item.label : String(path).split(/[\\/]/).pop()
            const isActive = path === active
            return jsxs(
              'span',
              {
                style: {
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 4,
                  flex: '0 0 auto',
                  padding: '3px 6px',
                  borderRadius: 6,
                  fontSize: '0.7rem',
                  cursor: 'pointer',
                  background: isActive ? 'var(--chrome-action-hover, rgba(127,127,127,0.16))' : 'transparent',
                  border: '1px solid var(--ui-stroke-secondary, rgba(127,127,127,0.2))',
                },
                children: [
                  jsx('span', { onClick: () => openTab(path), title: path, children: label }),
                  jsx('span', {
                    onClick: event => {
                      event.stopPropagation()
                      closeTab(path)
                    },
                    title: 'Close',
                    style: { opacity: 0.6, padding: '0 2px' },
                    children: '\u00d7',
                  }),
                ],
              },
              path,
            )
          }),
          ],
        }),
        jsx('div', {
          style: { flex: '1 1 auto', minHeight: 0, display: 'flex', flexDirection: 'column' },
          children: jsx(FileView, { ctx, session, artifact: activeArtifact, live: true }),
        }),
      ],
    })
  }
}

function HeaderButton({ label, onClick, icon, active, showLabel }) {
  return jsx('button', {
    type: 'button',
    title: label,
    'aria-label': label,
    onClick,
    style: {
      flex: '0 0 auto',
      display: 'inline-flex',
      alignItems: 'center',
      justifyContent: 'center',
      padding: '4px 6px',
      borderRadius: 6,
      cursor: 'pointer',
      background: active ? 'var(--chrome-action-hover, rgba(127,127,127,0.18))' : 'transparent',
      border: '1px solid transparent',
      color: 'inherit',
      opacity: active ? 1 : 0.7,
    },
    children: [
      jsx(Icon, { name: icon, size: '0.8rem' }),
      // A text label makes the control obvious; the arrow alone reads as noise
      // next to Refresh in a 420px header.
      showLabel
        ? jsx('span', { style: { marginLeft: 4, fontSize: '0.7rem' }, children: showLabel })
        : null,
    ],
  })
}

function HealthPanel({ ctx }) {
  const query = useRest(ctx, '/health', { enabled: true })
  const data = query.data
  const rows = data
    ? [
        ['ok', String(data.ok)],
        ['core loaded', String(data.core_loaded)],
        ['core error', data.core_error || '-'],
        ['hermes home', data.hermes_home || '-'],
        ['session db', data.session_db_ok ? 'ok' : `failed: ${data.session_db_error || '?'}`],
        ['python', data.python || '-'],
      ]
    : [['status', query.error || 'loading…']]

  return jsxs('div', {
    style: {
      margin: '0 10px 8px',
      padding: '8px 10px',
      borderRadius: 8,
      fontSize: '0.68rem',
      lineHeight: 1.5,
      background: 'var(--ui-surface-secondary, rgba(127,127,127,0.10))',
      border: '1px solid var(--ui-stroke-secondary, rgba(127,127,127,0.25))',
    },
    children: [
      jsx('div', { style: { fontWeight: 600, marginBottom: 4 }, children: 'Backend diagnostics' }),
      ...rows.map(([key, value]) =>
        jsxs(
          'div',
          {
            style: { display: 'flex', gap: 6 },
            children: [
              jsx('span', { style: { flex: '0 0 92px', opacity: 0.6 }, children: key }),
              jsx('span', { style: { wordBreak: 'break-all' }, children: value }),
            ],
          },
          key,
        ),
      ),
    ],
  })
}

// ── cross-surface handoff ───────────────────────────────────────────────────
/**
 * "Open this file in Athena", requested from anywhere in the app.
 *
 * A `fileCard.actions` contribution is mounted with NO props
 * (`contrib/react/slot.tsx` calls `render()` bare), so it cannot learn which
 * card it belongs to. The publishing surface stashes the path here and the
 * contribution reads it back — a one-slot mailbox, consumed by the pane.
 *
 * Module scope on purpose: the publisher (core, or a directive chip) and the
 * consumer (the pane) are different React trees with no shared context here.
 */
const openHandoff = {
  path: '',
  sessionId: '',
  token: 0,
}

const openHandoffSubs = new Set()

function requestOpen(path, sessionId) {
  const target = typeof path === 'string' ? path.trim() : ''
  if (!target) {
    return false
  }
  openHandoff.path = target
  openHandoff.sessionId = typeof sessionId === 'string' ? sessionId : ''
  // A monotonic token makes two requests for the SAME path distinct events, so
  // re-clicking the same file still re-opens it.
  openHandoff.token += 1
  for (const notify of Array.from(openHandoffSubs)) {
    try {
      notify(openHandoff)
    } catch {
      /* one bad subscriber must not stop the others */
    }
  }
  return true
}

/** Subscribe to open requests. Returns an unsubscribe function. */
function subscribeOpen(handler) {
  if (typeof handler !== 'function') {
    return () => {}
  }
  openHandoffSubs.add(handler)
  return () => {
    openHandoffSubs.delete(handler)
  }
}

/** The pending request, if any. Read by the pane on mount and on each request. */
function peekOpen() {
  return openHandoff.path ? openHandoff : null
}

function clearOpen() {
  openHandoff.path = ''
  openHandoff.sessionId = ''
}

/** Put a path in front of Athena and bring the pane forward. */
function openInAthena(path, sessionId) {
  if (!requestOpen(path, sessionId)) {
    return false
  }
  try {
    if (typeof host.revealPane === 'function') {
      host.revealPane(PANE_ID)
      return true
    }
    if (typeof host.navigate === 'function') {
      host.navigate(PAGE_PATH)
      return true
    }
  } catch {
    /* revealing is best-effort; the request is already queued either way */
  }
  return false
}

/**
 * "Athena" action rendered inside a transcript file card.
 *
 * Renders unconditionally. An earlier version returned `null` until the
 * focused-session atom had a value, which hid the button on exactly the cards
 * it was meant for and called a hook after an early return.
 */
function OpenInAthenaButton(context) {
  // The host passes `{ path, name }` for the row this action is rendered in
  // (changed-files-card mounts one slot per file). That is the authoritative
  // target — a slot has no other way to know which file it belongs to.
  //
  // The mailbox is the FALLBACK for hosts that mount this area without context
  // (an attachment card that carries a path but no per-row slot). peekOpen()
  // returns null in the normal mount state, so it must be tolerated.
  const fromSlot = context && typeof context.path === 'string' ? context.path : ''
  const [pending, setPending] = useState(() => (fromSlot ? null : peekOpen() || null))

  useEffect(() => {
    if (fromSlot) return undefined
    return subscribeOpen(next => setPending(next ? { ...next } : null))
  }, [fromSlot])

  const queued = pending && pending.path
  const target = fromSlot || queued || ''
  const label = target ? target.replace(/\\/g, '/').split('/').pop() : ''

  return jsx('button', {
    type: 'button',
    // The path is data, never markup — jsx children are text nodes.
    title: target
      ? `Open ${label} in Athena`
      : 'Open this session\u2019s artifacts in Athena',
    'aria-label': target ? `Open ${label} in Athena` : 'Open in Athena',
    onClick: event => {
      // The card is itself a button: never let this bubble into opening the
      // file's own preview.
      if (event && typeof event.stopPropagation === 'function') {
        event.stopPropagation()
      }
      if (typeof event?.preventDefault === 'function') {
        event.preventDefault()
      }
      try {
        if (target) {
          // `pending` is null whenever the path came from the slot, so the
          // session id must be read off it defensively — it is optional here
          // because the pane resolves the focused session itself.
          openInAthena(target, (pending && pending.sessionId) || '')
        } else if (typeof host.revealPane === 'function') {
          host.revealPane(PANE_ID)
        } else if (typeof host.navigate === 'function') {
          host.navigate(PAGE_PATH)
        }
      } catch {
        /* reveal is best-effort */
      }
    },
    style: {
      flex: '0 0 auto',
      display: 'inline-flex',
      alignItems: 'center',
      gap: 4,
      padding: '3px 8px',
      borderRadius: 6,
      fontSize: '0.7rem',
      fontWeight: 500,
      cursor: 'pointer',
      whiteSpace: 'nowrap',
      color: 'inherit',
      background: 'transparent',
      border: '1px solid var(--ui-stroke-secondary, rgba(127,127,127,0.3))',
    },
    children: [jsx(Icon, { name: 'book', size: '0.72rem' }), 'Athena'],
  })
}

/**
 * `::athena{path="…"}` — a chip the model can emit inside a message.
 *
 * Directive attributes are UNTRUSTED model output, so the path is validated
 * before it is used: absolute, no traversal, and no credential directory.
 */
function AthenaDirectiveChip({ attrs }) {
  const raw = attrs && typeof attrs === 'object' ? attrs.path : ''
  const path = typeof raw === 'string' ? raw.trim() : ''
  const usable = isSafeOpenPath(path)

  if (!usable) {
    // Render nothing rather than guessing at a malformed/hostile path.
    return null
  }

  const label = path.replace(/\\/g, '/').split('/').pop() || path
  return jsx(
    'button',
    {
      type: 'button',
      title: `Open ${label} in Athena`,
      'aria-label': `Open ${label} in Athena`,
      onClick: () => {
        try {
          openInAthena(path)
        } catch {
          /* best-effort */
        }
      },
      style: {
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        margin: '4px 0',
        padding: '4px 10px',
        borderRadius: 6,
        fontSize: '0.72rem',
        cursor: 'pointer',
        background: 'var(--ui-surface-secondary, rgba(127,127,127,0.12))',
        border: '1px solid var(--ui-stroke-secondary, rgba(127,127,127,0.3))',
        color: 'inherit',
      },
      children: [jsx(Icon, { name: 'book', size: '0.75rem' }), `Open ${label} in Athena`],
    },
    null,
  )
}

// ── registration ────────────────────────────────────────────────────────────
let apiCtx = null

function renderPane() {
  return jsx(AthenaView, { ctx: apiCtx, variant: 'pane' })
}

function renderPage() {
  return jsx(AthenaView, { ctx: apiCtx, variant: 'page' })
}

export default {
  id: ID,
  name: 'Athena',
  description:
    "Right-side panel listing the focused session's artifacts and opening them in-panel: tabs, rendered markdown and walkthroughs, code with line numbers, images, and sandboxed HTML preview.",
  defaultEnabled: true,
  register(ctx) {
    apiCtx = ctx

    ctx.registerMany([
      {
        // PRIMARY surface: a real right-zone tab beside Files. `placement` only
        // decides where ADOPTION puts a pane missing from the tree; `dock.enforce`
        // is what re-homes an already-placed one, and the anchor must be a real
        // registered pane id ('files') or enforcement silently no-ops.
        id: 'pane',
        area: PANES_AREA,
        title: 'Athena',
        data: {
          placement: 'right',
          collapsible: true,
          hideOnly: true,
          width: '520px',
          minWidth: '280px',
          maxWidth: '1400px',
          dock: { pane: 'files', pos: 'center', enforce: true },
          revealAliases: [ID],
          tabTitle: () => jsx(Icon, { name: 'book', size: '0.8rem' }),
          tabTitleText: () => 'Athena',
        },
        render: renderPane,
      },
      {
        // SECONDARY surface: the same component as a full page.
        id: 'page',
        area: ROUTES_AREA,
        title: 'Athena',
        data: { path: PAGE_PATH },
        render: renderPage,
      },
      {
        // …reached from a sidebar nav row.
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        order: 44,
        data: { path: PAGE_PATH, label: 'Athena', codicon: 'book' },
      },
      {
        // A quick way in from any inline file card in the transcript.
        id: 'file-card-action',
        area: FILE_CARD_ACTIONS_AREA,
        order: 10,
        title: 'Athena',
        render: ctx => jsx(OpenInAthenaButton, { context: ctx }),
      },
      {
        // `::athena{path="…"}` — an in-message chip the agent can emit so a
        // produced file is one click from the Athena viewer.
        id: 'directive',
        area: TRANSCRIPT_DIRECTIVE_AREA,
        data: {
          // Namespaced so a future core/plugin `athena` cannot collide: first
          // registration wins on a name clash.
          name: 'athena',
          render: ({ attrs }) => jsx(AthenaDirectiveChip, { attrs }),
        },
      },
    ])

    ctx.onDispose(() => {
      apiCtx = null
    })
  },
}
