import type { ConfirmRequest } from './bridge'
import { McpApproveCard } from './McpCard'

interface NetworkCard {
  containment?: string
  note?: string
  hosts?: Array<{
    host: string
    scheme: string
    methods: string[]
    paths: string[]
    maxResponseBytes: number
    requestsPerHour: number
    writes?: boolean
    highBudget?: boolean
  }>
  secrets?: Array<{ name: string; header: string; hosts: string[] }>
}

// The Blackwall card says what a generated tool may do in words, not JSON.
function describeCapabilities(raw: unknown): string {
  const caps = (raw ?? {}) as Record<string, unknown>
  const flags = ['network', 'read_files', 'write_files', 'subprocess', 'screen'].filter(
    (name) => caps[name] === true,
  )
  return flags.length ? flags.join(' · ') : 'no capabilities'
}

function describeNetwork(card: NetworkCard): string {
  const lines = [`network · ${card.containment ?? 'declared'}`]
  for (const rule of card.hosts ?? []) {
    const kb = Math.round((rule.maxResponseBytes ?? 0) / 1024)
    const paths = (rule.paths ?? []).join(', ')
    const marks = [
      rule.highBudget ? 'high budget' : '',
      rule.writes ? 'writes: first call confirmed' : '',
    ].filter(Boolean)
    lines.push(
      `${(rule.methods ?? []).join('/')} ${rule.scheme}://${rule.host}${paths} · ${rule.requestsPerHour}/h · ${kb} KB${marks.map((mark) => ` · ${mark}`).join('')}`,
    )
  }
  for (const secret of card.secrets ?? []) {
    lines.push(`uses ${secret.name} → ${secret.header} (${(secret.hosts ?? []).join(', ')})`)
  }
  if (card.note) lines.push(card.note)
  return lines.join('\n')
}

export function ConfirmModal({
  confirming,
  onAnswer,
}: {
  confirming: ConfirmRequest
  onAnswer: (approved: boolean, tiers?: Record<string, number>) => void
}) {
  // Blackwall proposals carry their own review material inside args. Keep the
  // generic JSON view for ordinary tool calls, but label a tool-version card.
  const blackwall =
    confirming.tool === 'tool.propose'
      ? (confirming.args as Record<string, unknown>)
      : null
  const networkCard = (blackwall?.network ?? null) as NetworkCard | null
  // A server card needs its own body (command, env, a tier each) and its own
  // actions, so the plain approve/deny underneath is suppressed while it shows.
  const mcpCard =
    confirming.tool === 'mcp.register'
      ? {
          servers: confirming.servers ?? [],
          skipped: confirming.skipped ?? [],
          notes: confirming.notes ?? [],
        }
      : null

  return (
    <div className="confirm">
      <div className="confirm-head">
        <span className="confirm-tool">{confirming.tool}</span>
        <span className="confirm-tier" data-tier={confirming.tier}>
          {confirming.tierName}
        </span>
        <span className="spacer" />
        <span className="confirm-timer">{confirming.timeout}s</span>
      </div>
      <p className="confirm-reason">{confirming.reason}</p>
      {blackwall ? (
        <div className="confirm-blackwall">
          <div className="confirm-blackwall-head">
            <strong>
              {String(blackwall.name)} {String(blackwall.version)}
            </strong>
            <span>{describeCapabilities(blackwall.capabilities)}</span>
          </div>
          {networkCard ? (
            <pre className="confirm-args confirm-network">
              {describeNetwork(networkCard)}
            </pre>
          ) : null}
          <pre className="confirm-args">
            {JSON.stringify(blackwall.audit ?? [], null, 2)}
          </pre>
          <pre className="confirm-args">
            {JSON.stringify(blackwall.review ?? {}, null, 2)}
          </pre>
          <pre className="confirm-args">{String(blackwall.tests ?? '')}</pre>
          <pre className="confirm-args">{String(blackwall.testSource ?? '')}</pre>
          <pre className="confirm-args confirm-diff">
            {String(blackwall.diff ?? '')}
          </pre>
        </div>
      ) : mcpCard ? (
        <McpApproveCard
          key={confirming.confirmId}
          servers={mcpCard.servers}
          skipped={mcpCard.skipped}
          notes={mcpCard.notes}
          onAnswer={onAnswer}
        />
      ) : (
        <pre className="confirm-args">{JSON.stringify(confirming.args, null, 2)}</pre>
      )}
      {mcpCard ? null : (
        <div className="confirm-actions">
          <button className="deny" onClick={() => onAnswer(false)}>
            deny
          </button>
          <button className="approve" onClick={() => onAnswer(true)}>
            approve
          </button>
        </div>
      )}
    </div>
  )
}
