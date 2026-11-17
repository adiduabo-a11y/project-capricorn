import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { capri, type ToolTry } from './bridge'
import { MaskedValue } from './MaskedValue'

/** One configured server, as the daemon reports it. */
type Server = {
  name: string
  command: string
  args: string[]
  cwd: string
  env: Record<string, string>
  enabled: boolean
  tier: number
  running: boolean
  tools: string[]
  error: string
}

type List = { enabled: boolean; servers: Server[] }
type Saved = List & { ok: boolean; started?: number; changed?: boolean }

/** What mcp.import answers: the rows it wrote, and what it could not use. */
type Imported = List & {
  ok: boolean
  imported: string[]
  skipped: { name: string; reason: string }[]
  notes: string[]
}

/** The last import, kept on screen until the next one. */
type Report = {
  error?: string
  imported: string[]
  skipped: { name: string; reason: string }[]
  notes: string[]
}

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

/** One env variable while it is being edited. */
type EnvRow = { key: string; value: string }

/** The editor starts from what the daemon reports, in config order. */
function envRows(env: Record<string, string> | undefined): EnvRow[] {
  return Object.entries(env ?? {}).map(([key, value]) => ({ key, value }))
}

/** The variables a form is holding: a name left blank is not one. */
function envFrom(rows: EnvRow[]): Record<string, string> {
  const env: Record<string, string> = {}
  for (const row of rows) {
    const key = row.key.trim()
    if (key !== '') env[key] = row.value
  }
  return env
}

/** The rows of an environment: add, change, drop, and let the caller save. */
function EnvEditor({
  rows,
  onChange,
  disabled,
  children,
}: {
  rows: EnvRow[]
  onChange: (rows: EnvRow[]) => void
  disabled?: boolean
  children?: ReactNode
}) {
  const set = (index: number, patch: Partial<EnvRow>) =>
    onChange(rows.map((row, at) => (at === index ? { ...row, ...patch } : row)))
  return (
    <div className="mcp-env">
      {rows.map((row, index) => (
        <div className="mcp-env-row" key={index}>
          <input
            className="mcp-env-key"
            value={row.key}
            placeholder="NAME"
            spellCheck={false}
            onChange={(event) => set(index, { key: event.target.value })}
          />
          <input
            className="mcp-env-val"
            value={row.value}
            placeholder="value"
            spellCheck={false}
            onChange={(event) => set(index, { value: event.target.value })}
          />
          <button
            className="mcp-link"
            title="drop this variable"
            onClick={() => onChange(rows.filter((_row, at) => at !== index))}
            disabled={disabled}
          >
            drop
          </button>
        </div>
      ))}
      <div className="mcp-env-row">
        <button
          className="mem-sweep"
          onClick={() => onChange([...rows, { key: '', value: '' }])}
          disabled={disabled}
        >
          add variable
        </button>
        {children ? (
          <>
            <span className="spacer" />
            {children}
          </>
        ) : null}
      </div>
    </div>
  )
}

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

/** What the daemon's answer means for the row just saved, in a few words. */
function outcome(next: Saved, name: string): string {
  if (next.changed === false) return 'nothing changed'
  const row = next.servers.find((server) => server.name === name)
  if (!row?.enabled) return 'stopped'
  // The row right below already prints the error in full, stderr and all.
  if (row.error) return 'did not start'
  return 'restarted'
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
  // A new server's env is folded away until it is asked for: most servers need
  // none, and a token field on every add is an invitation to paste one twice.
  const [draftEnv, setDraftEnv] = useState<EnvRow[]>([])
  const [draftEnvOpen, setDraftEnvOpen] = useState(false)
  const [paste, setPaste] = useState('')
  // One server's env at a time: a token typed into two rows is a mistake
  // waiting to happen, and the save is explicit so no keystroke restarts it.
  const [envEdit, setEnvEdit] = useState<{ name: string; rows: EnvRow[] } | null>(null)
  const [report, setReport] = useState<Report | null>(null)
  // What the last try of the add row answered. Nothing is saved by it.
  const [probe, setProbe] = useState<ToolTry | null>(null)

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
    async (name: string, patch: Record<string, unknown>): Promise<boolean> => {
      setBusy(name)
      setNote('')
      try {
        const next = await capri.call<Saved>('mcp.save', { name, ...patch })
        apply(next, `${name} saved — ${outcome(next, name)}`)
        return true
      } catch (err) {
        setNote(message(err))
        return false
      } finally {
        setBusy(null)
      }
    },
    [apply],
  )

  // Saved whole, the way the daemon reads it: a name left blank is simply not
  // part of the environment, and a saved one replaces what was there.
  const saveEnv = useCallback(
    async (name: string) => {
      if (envEdit === null || envEdit.name !== name) return
      if (await save(name, { env: envFrom(envEdit.rows) })) setEnvEdit(null)
    },
    [envEdit, save],
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
      // Sent with the rest of the row: a server that needs a token starts on
      // the first try instead of being added, failing, and then edited.
      const next = await capri.call<Saved>('mcp.save', {
        name,
        command: draft.command.trim(),
        args: splitArgs(draft.args),
        tier: draft.tier,
        enabled: true,
        env: envFrom(draftEnv),
      })
      const row = next.servers.find((server) => server.name === name)
      apply(next, `${name} added — ${outcome(next, name)}`)
      if (!row?.error) {
        setDraft({ name: '', command: '', args: '', tier: 2 })
        setDraftEnv([])
        setDraftEnvOpen(false)
        setProbe(null)
      }
    } catch (err) {
      setNote(message(err))
    } finally {
      setBusy(null)
    }
  }, [apply, draft, draftEnv])

  // A draft that has never been saved: started once, listed, stopped. The only
  // way to check a command and its token before they become a row.
  const tryDraft = useCallback(async () => {
    const command = draft.command.trim()
    if (!command) return
    setBusy('try')
    setNote('')
    setProbe(null)
    try {
      setProbe(
        await capri.call<ToolTry>('mcp.try', {
          // No name yet: the daemon labels the failure with the command.
          name: draft.name.trim(),
          command,
          args: splitArgs(draft.args),
          env: envFrom(draftEnv),
        }),
      )
    } catch (err) {
      setProbe({ ok: false, tools: [], error: message(err) })
    } finally {
      setBusy(null)
    }
  }, [draft, draftEnv])

  const importConfig = useCallback(async () => {
    const text = paste.trim()
    if (!text) return
    setBusy('import')
    setNote('')
    setReport(null)
    try {
      const next = await capri.call<Imported>('mcp.import', { config: text })
      setList({ enabled: next.enabled, servers: next.servers })
      setReport({ imported: next.imported, skipped: next.skipped, notes: next.notes })
      setNote(
        next.imported.length
          ? `imported ${next.imported.length} server${next.imported.length === 1 ? '' : 's'} — restarted with ${next.imported.length === 1 ? 'it' : 'them'}`
          : 'nothing was imported',
      )
      if (next.imported.length) setPaste('')
    } catch (err) {
      // A bad paste is a report line, not a lost one: the text stays in the box.
      setReport({ imported: [], skipped: [], notes: [], error: message(err) })
    } finally {
      setBusy(null)
    }
  }, [paste])

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
  // The add form's env: one blank row to start with, and whatever was typed after.
  const draftRows = draftEnv.length > 0 ? draftEnv : [{ key: '', value: '' }]

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
                {envEdit?.name === server.name ? (
                  <EnvEditor
                    rows={envEdit.rows}
                    onChange={(rows) => setEnvEdit({ name: server.name, rows })}
                    disabled={disabled}
                  >
                    <button
                      className="mem-sweep"
                      onClick={() => void saveEnv(server.name)}
                      disabled={disabled}
                    >
                      save env
                    </button>
                    <button className="mcp-link" onClick={() => setEnvEdit(null)}>
                      cancel
                    </button>
                  </EnvEditor>
                ) : (
                  <div className="mcp-env">
                    {Object.entries(server.env ?? {}).map(([key, value]) => (
                      <div className="mcp-env-line" key={key}>
                        <span className="mcp-env-name">{key}</span>
                        <MaskedValue text={value} />
                      </div>
                    ))}
                    <div className="mcp-env-row">
                      <button
                        className="mcp-link"
                        disabled={disabled}
                        onClick={() =>
                          setEnvEdit({ name: server.name, rows: envRows(server.env) })
                        }
                      >
                        {Object.keys(server.env ?? {}).length > 0 ? 'edit env' : 'add env'}
                      </button>
                    </div>
                  </div>
                )}
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
        <button
          className="mcp-link"
          onClick={() => setDraftEnvOpen(!draftEnvOpen)}
          disabled={!online}
          title="environment variables for the new server"
        >
          {draftEnvOpen ? 'hide env' : 'env'}
        </button>
        <button
          className="mcp-link"
          onClick={() => void tryDraft()}
          disabled={!online || busy !== null || draft.command.trim() === ''}
          title="start this command once, without saving it"
        >
          try
        </button>
        <button className="mem-sweep" onClick={() => void add()} disabled={!online || !ready}>
          add
        </button>
      </div>
      {probe ? (
        probe.ok ? (
          <p className="mem-note">
            try: {probe.tools.length} tool{probe.tools.length === 1 ? '' : 's'}
            {probe.tools.length > 0
              ? ` — ${probe.tools
                  .slice(0, 8)
                  .map((tool) => tool.name)
                  .join(' · ')}`
              : ' — none advertised'}
          </p>
        ) : (
          <p className="mcp-error">try: {probe.error}</p>
        )
      ) : null}
      {draftEnvOpen ? <EnvEditor rows={draftRows} onChange={setDraftEnv} /> : null}
      <div className="mcp-import">
        <div className="mcp-head">
          <span className="mcp-title">Import a config</span>
          <span className="mcp-sub">paste one from Claude Desktop</span>
        </div>
        <textarea
          className="mcp-paste"
          value={paste}
          spellCheck={false}
          placeholder={
            '{\n  "mcpServers": {\n    "fs": {\n      "command": "npx",\n      "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],\n      "env": { "API_KEY": "…" }\n    }\n  }\n}'
          }
          onChange={(event) => setPaste(event.target.value)}
        />
        <div className="mcp-add">
          <button
            className="mem-sweep"
            onClick={() => void importConfig()}
            disabled={!online || busy !== null || paste.trim() === ''}
          >
            import
          </button>
          <span className="mcp-sub">stdio servers only</span>
        </div>
      </div>
      {report ? (
        <div className="mcp-report">
          {report.error ? <div className="mcp-import-line bad">{report.error}</div> : null}
          {report.imported.map((name) => (
            <div className="mcp-import-line ok" key={`in-${name}`}>
              {name} imported
            </div>
          ))}
          {report.skipped.map((entry) => (
            <div className="mcp-import-line bad" key={`sk-${entry.name}`}>
              {entry.name} — {entry.reason}
            </div>
          ))}
          {report.notes.map((text) => (
            <p className="mcp-note" key={`note-${text}`}>
              {text}
            </p>
          ))}
        </div>
      ) : null}
      {note ? <p className="mem-note">{note}</p> : null}
    </section>
  )
}
