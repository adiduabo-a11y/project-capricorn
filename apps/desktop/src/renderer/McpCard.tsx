import { useState } from 'react'
import { capri, type ToolTry } from './bridge'
import { MaskedValue } from './MaskedValue'

/** One server as the daemon stages it for approval. */
export type McpServerDraft = {
  name: string
  command: string
  args: string[]
  env: Record<string, string>
  cwd: string
  tier: number
}

export const TIERS = [
  { value: 0, label: 'tier 0 — runs silently' },
  { value: 1, label: 'tier 1 — runs, mentions it' },
  { value: 2, label: 'tier 2 — asks first' },
  { value: 3, label: 'tier 3 — never runs' },
] as const

/** Every server a third party wrote starts where a third party belongs. */
export const DEFAULT_TIER = 2

/** One server's try, while it is running and after it answers. */
type Probe = { state: 'busy' } | { state: 'done'; ok: boolean; text: string }

/** What a try answered, in one line under the command it started. */
function describe(tried: ToolTry): string {
  if (!tried.ok) return tried.error
  const names = tried.tools.map((tool) => tool.name)
  return names.length > 0
    ? `${names.length} tool${names.length === 1 ? '' : 's'} — ${names.slice(0, 8).join(' · ')}`
    : 'started, and advertises no tools'
}

type Props = {
  servers: McpServerDraft[]
  skipped: { name: string; reason: string }[]
  notes: string[]
  onAnswer: (approved: boolean, tiers: Record<string, number>) => void
}

/**
 * The approval for adding MCP servers: the whole command, its environment, and
 * a tier per server. A server runs with the daemon's own rights, and its env
 * can hold a key, so this is the card that has to show both before a yes.
 */
export function McpApproveCard({ servers, skipped, notes, onAnswer }: Props) {
  const [tiers, setTiers] = useState<Record<string, number>>(() =>
    Object.fromEntries(servers.map((server) => [server.name, DEFAULT_TIER])),
  )

  const choose = (name: string, tier: number) =>
    setTiers((prev) => ({ ...prev, [name]: tier }))

  // Starting it once before a yes is the difference between approving a command
  // and approving one that works. Nothing is written and no tier is needed.
  const [probes, setProbes] = useState<Record<string, Probe>>({})

  const runTry = async (server: McpServerDraft) => {
    setProbes((prev) => ({ ...prev, [server.name]: { state: 'busy' } }))
    try {
      const tried = await capri.call<ToolTry>('mcp.try', {
        name: server.name,
        command: server.command,
        args: server.args,
        cwd: server.cwd,
        env: server.env,
      })
      setProbes((prev) => ({
        ...prev,
        [server.name]: { state: 'done', ok: tried.ok, text: describe(tried) },
      }))
    } catch (err) {
      setProbes((prev) => ({
        ...prev,
        [server.name]: {
          state: 'done',
          ok: false,
          text: err instanceof Error ? err.message : String(err),
        },
      }))
    }
  }

  return (
    <div className="confirm-mcp">
      <div className="confirm-mcp-head">
        <strong>
          {servers.length} server{servers.length === 1 ? '' : 's'}
        </strong>
        <span>written to the config and started on approval</span>
      </div>

      {servers.map((server) => {
        const probe = probes[server.name]
        return (
          <div className="confirm-mcp-server" key={server.name}>
            <div className="confirm-mcp-line">
              <span className="confirm-mcp-name">{server.name}</span>
              <span className="spacer" />
              <select
                className="confirm-mcp-tier"
                value={tiers[server.name] ?? DEFAULT_TIER}
                title="how much this server's tools are trusted"
                onChange={(event) => choose(server.name, Number(event.target.value))}
              >
                {TIERS.map((tier) => (
                  <option key={tier.value} value={tier.value}>
                    {tier.label}
                  </option>
                ))}
              </select>
              <button
                className="mcp-link"
                onClick={() => void runTry(server)}
                disabled={probe?.state === 'busy'}
                title="start it once, without registering it"
              >
                {probe?.state === 'busy' ? 'trying…' : 'try'}
              </button>
            </div>
            <pre className="confirm-args confirm-mcp-command">
              {server.command}
              {server.args.length > 0 ? ` ${server.args.join(' ')}` : ''}
            </pre>
            {server.cwd ? <p className="confirm-mcp-where">runs in {server.cwd}</p> : null}
            {Object.keys(server.env).length > 0 ? (
              <div className="confirm-args confirm-mcp-env">
                {Object.entries(server.env).map(([key, value]) => (
                  <div key={key}>
                    {key}=<MaskedValue text={value} />
                  </div>
                ))}
              </div>
            ) : null}
            {probe && probe.state === 'done' ? (
              <p className={probe.ok ? 'confirm-mcp-note' : 'confirm-mcp-skip'}>
                {probe.text}
              </p>
            ) : null}
          </div>
        )
      })}

      {skipped.map((entry) => (
        <div className="confirm-mcp-skip" key={`skip-${entry.name}`}>
          {entry.name} — {entry.reason}
        </div>
      ))}

      {notes.map((note) => (
        <p className="confirm-mcp-note" key={note}>
          {note}
        </p>
      ))}

      <div className="confirm-actions">
        <button className="deny" onClick={() => onAnswer(false, {})}>
          deny
        </button>
        <button className="approve" onClick={() => onAnswer(true, tiers)}>
          approve and register
        </button>
      </div>
    </div>
  )
}
