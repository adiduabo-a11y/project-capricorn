import { useCallback, useEffect, useState } from 'react'
import { capri } from './bridge'

/** One configured server, as the daemon reports it. */
type Server = {
  name: string
  command: string
  args: string[]
  cwd: string
  enabled: boolean
  tier: number
  running: boolean
  tools: string[]
  error: string
}

type List = { enabled: boolean; servers: Server[] }
type Saved = List & { ok: boolean; started?: number }

const TIERS = [
  { value: 0, label: 'tier 0 — runs silently' },
  { value: 1, label: 'tier 1 — runs, mentions it' },
  { value: 2, label: 'tier 2 — asks first' },
  { value: 3, label: 'tier 3 — never runs' },
]

/** Split an argument line, keeping quoted runs together. */
function splitArgs(text: string): string[] {
  const out: string[] = []
  const re = /"([^"]*)"|'([^']*)'|(\S+)/g
  let match: RegExpExecArray | null
  while ((match = re.exec(text)) !== null) out.push(match[1] ?? match[2] ?? match[3] ?? '')
  return out.filter((arg) => arg !== '')
}

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

function statusOf(server: Server): { text: string; kind: string } {
  if (server.error) return { text: 'failed', kind: 'bad' }
  if (!server.enabled) return { text: 'off', kind: '' }
  if (!server.running) return { text: 'not running', kind: 'bad' }
  const count = server.tools.length
  return { text: `${count} tool${count === 1 ? '' : 's'}`, kind: 'ok' }
}

export function McpPanel({ online }: { online: boolean }) {
  const [list, setList] = useState<List | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [note, setNote] = useState('')
  const [draft, setDraft] = useState({ name: '', command: '', args: '', tier: 2 })

  const load = useCallback(async () => {
    try {
      setList(await capri.call<List>('mcp.servers'))
    } catch (err) {
      setNote(message(err))
    }
  }, [])

  useEffect(() => {
    if (online) void load()
  }, [load, online])

  const apply = useCallback((next: Saved, said: string) => {
    setList({ enabled: next.enabled, servers: next.servers })
    setNote(said)
  }, [])

  const save = useCallback(
    async (name: string, patch: Record<string, unknown>) => {
      setBusy(name)
      setNote('')
      try {
        apply(
          await capri.call<Saved>('mcp.save', { name, ...patch }),
          `${name} saved — restarted with it`,
        )
      } catch (err) {
        setNote(message(err))
      } finally {
        setBusy(null)
      }
    },
    [apply],
  )

  const remove = useCallback(
    async (name: string) => {
      setBusy(name)
      setNote('')
      try {
        apply(await capri.call<Saved>('mcp.remove', { name }), `${name} removed`)
      } catch (err) {
        setNote(message(err))
      } finally {
        setBusy(null)
      }
    },
    [apply],
  )

  const reload = useCallback(async () => {
    setBusy('all')
    setNote('')
    try {
      const next = await capri.call<Saved>('mcp.reload')
      apply(next, `retried every server — ${next.started ?? 0} came up`)
    } catch (err) {
      setNote(message(err))
    } finally {
      setBusy(null)
    }
  }, [apply])

  const add = useCallback(async () => {
    const name = draft.name.trim()
    setBusy(name || 'add')
    setNote('')
    try {
      const next = await capri.call<Saved>('mcp.save', {
        name,
        command: draft.command.trim(),
        args: splitArgs(draft.args),
        tier: draft.tier,
        enabled: true,
      })
      apply(next, `${name} added`)
      const row = next.servers.find((server) => server.name === name)
      if (row?.error) setNote(`${name} was saved but did not start: ${row.error}`)
      else setDraft({ name: '', command: '', args: '', tier: 2 })
    } catch (err) {
      setNote(message(err))
    } finally {
      setBusy(null)
    }
  }, [apply, draft])

  const setGlobal = useCallback(async (enabled: boolean) => {
    setBusy('global')
    setNote('')
    try {
      await capri.call('config.set', { path: 'mcp.enabled', value: enabled })
      await reload()
    } catch (err) {
      setNote(message(err))
      setBusy(null)
    }
  }, [reload])

  const ready = draft.name.trim() !== '' && draft.command.trim() !== '' && busy === null

  return (
    <section className="mcp-panel">
      <div className="mcp-head">
        <span className="mcp-title">MCP servers</span>
        <span className="mcp-sub">extra tools, one process each</span>
        <span className="spacer" />
        <button className="mem-sweep" onClick={() => void reload()} disabled={!online || busy !== null}>
          retry all
        </button>
      </div>
      {!online ? <p className="empty">MCP servers need the core online.</p> : null}
      {online && list && !list.enabled ? (
        <p className="mem-note">
          MCP is switched off in the config.{' '}
          <button className="mcp-link" onClick={() => void setGlobal(true)} disabled={busy !== null}>
            turn it on
          </button>
        </p>
      ) : null}
      {online && list
        ? list.servers.map((server) => {
            const status = statusOf(server)
            const disabled = busy !== null
            return (
              <div className={`mcp-row${server.error ? ' mcp-bad' : ''}`} key={server.name}>
                <div className="mcp-line">
                  <span className="mcp-name">{server.name}</span>
                  <span className={`mcp-chip${status.kind ? ` ${status.kind}` : ''}`}>{status.text}</span>
                  <span className="spacer" />
                  <label className="mcp-toggle" title="whether this server is started at all">
                    <input
                      type="checkbox"
                      checked={server.enabled}
                      disabled={disabled}
                      onChange={(event) => void save(server.name, { enabled: event.target.checked })}
                    />
                    on
                  </label>
                  <select
                    className="mcp-tier"
                    value={server.tier}
                    disabled={disabled}
                    title="how much this server's tools are trusted"
                    onChange={(event) => void save(server.name, { tier: Number(event.target.value) })}
                  >
                    {TIERS.map((tier) => (
                      <option key={tier.value} value={tier.value}>
                        {tier.label}
                      </option>
                    ))}
                  </select>
                  <button
                    className="mem-sweep"
                    onClick={() => void remove(server.name)}
                    disabled={disabled}
                  >
                    remove
                  </button>
                </div>
                <div className="mcp-cmd">
                  {server.command} {server.args.join(' ')}
                </div>
                {server.error ? <div className="mcp-error">{server.error}</div> : null}
                {server.running && server.tools.length > 0 ? (
                  <div className="mcp-tools">{server.tools.join(' · ')}</div>
                ) : null}
              </div>
            )
          })
        : null}
      {online && list && list.servers.length === 0 ? (
        <p className="empty">No servers yet. Add one below.</p>
      ) : null}
      <div className="mcp-add">
        <input
          value={draft.name}
          placeholder="name"
          onChange={(event) => setDraft({ ...draft, name: event.target.value })}
        />
        <input
          value={draft.command}
          placeholder="command"
          onChange={(event) => setDraft({ ...draft, command: event.target.value })}
        />
        <input
          className="mcp-args"
          value={draft.args}
          placeholder="arguments"
          onChange={(event) => setDraft({ ...draft, args: event.target.value })}
        />
        <select
          className="mcp-tier"
          value={draft.tier}
          onChange={(event) => setDraft({ ...draft, tier: Number(event.target.value) })}
        >
          {TIERS.map((tier) => (
            <option key={tier.value} value={tier.value}>
              {tier.label}
            </option>
          ))}
        </select>
        <button className="mem-sweep" onClick={() => void add()} disabled={!online || !ready}>
          add
        </button>
      </div>
      {note ? <p className="mem-note">{note}</p> : null}
    </section>
  )
}
