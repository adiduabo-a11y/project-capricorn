import { useCallback, useEffect, useState } from 'react'
import { capri } from './bridge'

/** What notes.status answers: the folder, the notes, and what is indexed. */
type Status = {
  vault: string
  exists: boolean
  notes: number
  indexed: number
  chunks: number
  lastIndexed: number
  collection: string
  recall: boolean
  /** A pass in the background: the panel polls this until it stops. */
  indexing?: {
    running: boolean
    done: number
    total: number
    startedAt: number
    error: string
  }
  error: string
  removed?: number
  report?: {
    added: number
    updated: number
    unchanged: number
    removed: number
    chunks: number
    errors: string[]
  }
}

function when(ms: number): string {
  if (!ms) return 'never indexed'
  const seconds = Math.round((Date.now() - ms) / 1000)
  if (seconds < 90) return 'indexed just now'
  if (seconds < 3600) return `indexed ${Math.round(seconds / 60)} min ago`
  if (seconds < 86_400) return `indexed ${Math.round(seconds / 3600)} h ago`
  return `indexed ${Math.round(seconds / 86_400)} d ago`
}

function said(next: Status): string {
  if (next.indexing?.running) {
    return next.indexing.total
      ? `indexing ${next.indexing.total} notes in the background`
      : 'indexing the vault in the background'
  }
  if (next.indexing?.error) return `the index stopped: ${next.indexing.error}`
  const report = next.report
  if (!report) {
    if (next.removed !== undefined) {
      return `vault removed, ${next.removed} note${next.removed === 1 ? '' : 's'} unindexed`
    }
    return `indexed ${next.indexed} note${next.indexed === 1 ? '' : 's'}`
  }
  const parts = [`${report.added} new`, `${report.updated} changed`, `${report.removed} gone`]
  if (report.unchanged) parts.push(`${report.unchanged} unchanged`)
  return `indexed ${next.indexed} note${next.indexed === 1 ? '' : 's'}: ${parts.join(', ')}`
}

/**
 * Settings → Notes: the one folder she keeps notes in.
 *
 * The panel owns nothing: the daemon holds the path in its config, indexes the
 * markdown, and answers with the counts, so re-opening Settings shows what is
 * actually indexed rather than what the window last saw.
 */
export function NotesPanel({ online, open }: { online: boolean; open: boolean }) {
  const [status, setStatus] = useState<Status | null>(null)
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState('')

  const load = useCallback(async () => {
    try {
      setStatus(await capri.call<Status>('notes.status', {}))
    } catch (err) {
      setNote(err instanceof Error ? err.message : String(err))
    }
  }, [])

  useEffect(() => {
    if (online && open) void load()
  }, [load, online, open])

  // A background pass, watched rather than waited on. The interval exists only
  // while `indexing.running` is true, so an idle panel makes no calls.
  const running = status?.indexing?.running ?? false
  useEffect(() => {
    if (!online || !open || !running) return
    let live = true
    const tick = async () => {
      try {
        const next = await capri.call<Status>('notes.status', {})
        if (!live) return
        setStatus(next)
        if (!next.indexing?.running) setNote(said(next))
      } catch {
        // A dropped call is not a reason to stop watching: the next tick tries
        // again, and the pass keeps going in the core either way.
      }
    }
    const timer = window.setInterval(() => void tick(), 600)
    return () => {
      live = false
      window.clearInterval(timer)
    }
  }, [online, open, running])

  const run = useCallback(async (label: string, work: () => Promise<Status>) => {
    setBusy(true)
    setNote(label)
    try {
      const next = await work()
      setStatus(next)
      setNote(said(next))
    } catch (err) {
      setNote(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }, [])

  const choose = useCallback(async () => {
    const folder = await capri.chooseFolder('Choose your Obsidian vault')
    if (!folder) return
    await run('reading the vault…', () => capri.call<Status>('notes.set', { path: folder }))
  }, [run])

  const index = useCallback(async () => {
    await run('indexing…', () => capri.call<Status>('notes.index', {}))
  }, [run])

  const remove = useCallback(async () => {
    await run('removing…', () => capri.call<Status>('notes.set', { path: '' }))
  }, [run])

  const toggle = useCallback(
    async (on: boolean) => {
      setBusy(true)
      setNote(on ? 'her wiki: every turn starts with the notes that match' : 'notes are only read when she looks for them')
      try {
        await capri.call('config.set', { path: 'notes.recall', value: on })
        await load()
      } catch (err) {
        setNote(err instanceof Error ? err.message : String(err))
      } finally {
        setBusy(false)
      }
    },
    [load],
  )

  const vault = status?.vault ?? ''
  const counts = status
    ? !status.exists
      ? 'that folder is not there any more'
      : status.indexing?.running
        ? status.indexing.total
          ? `indexing ${status.indexing.done} of ${status.indexing.total} notes…`
          : 'indexing the vault…'
        : `${status.notes} note${status.notes === 1 ? '' : 's'} in the folder, ${status.indexed} indexed · ${when(status.lastIndexed)}`
    : 'reading…'

  return (
    <section className="mcp-panel">
      <div className="mcp-head">
        <span className="mcp-title">Notes</span>
        <span className="mcp-sub">her notebook, in your Obsidian vault</span>
        <span className="spacer" />
        <button
          className="mem-sweep"
          onClick={() => void choose()}
          disabled={!online || busy || running}
        >
          {vault ? 'change folder' : 'choose vault'}
        </button>
      </div>
      {!online ? <p className="empty">Notes need the core online.</p> : null}
      {online ? (
        <div className="mcp-row">
          <div className="mcp-line">
            <span className="mcp-name">{vault || 'no vault chosen yet'}</span>
          </div>
          <div className="mcp-line">
            <span className="mcp-sub">{counts}</span>
            <span className="spacer" />
            <button
              className="mcp-link"
              onClick={() => void index()}
              disabled={!status?.exists || busy || running}
            >
              index now
            </button>
            {vault ? (
              <button className="mcp-link" onClick={() => void remove()} disabled={busy}>
                remove vault
              </button>
            ) : null}
          </div>
          {vault ? (
            <label className="set-toggle">
              <span>
                <span className="set-label">Read it every turn</span>
                <span className="set-help">
                  Treat the vault as her wiki: the notes that answer each request come with it,
                  and she writes durable things back into it while you are asked first.
                </span>
              </span>
              <input
                type="checkbox"
                checked={status?.recall ?? true}
                disabled={busy}
                onChange={(event) => void toggle(event.target.checked)}
              />
            </label>
          ) : null}
        </div>
      ) : null}
      <p className="mem-note">
        she can search and read these notes; writing one asks you first, and the index is
        markdown only — Obsidian&apos;s own folders are never touched.
      </p>
      {status?.error ? <p className="mem-note">{status.error}</p> : null}
      {note ? <p className="mem-note">{note}</p> : null}
    </section>
  )
}
