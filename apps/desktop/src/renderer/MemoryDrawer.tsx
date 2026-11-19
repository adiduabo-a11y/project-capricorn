import type { MemoryItem } from './bridge'

export function MemoryDrawer({
  online,
  query,
  onQueryChange,
  busy,
  note,
  memories,
  picks,
  onSweep,
  onMergePicked,
  onForgetPicked,
  onClearPicks,
  onTogglePick,
  onForget,
}: {
  online: boolean
  query: string
  onQueryChange: (value: string) => void
  busy: boolean
  note: string
  memories: MemoryItem[]
  picks: number[]
  onSweep: () => void
  onMergePicked: () => void
  onForgetPicked: () => void
  onClearPicks: () => void
  onTogglePick: (id: number) => void
  onForget: (id: number) => void
}) {
  return (
    <div className="memories">
      <div className="mem-search">
        <input
          value={query}
          onChange={(event) => onQueryChange(event.target.value)}
          placeholder="search the shard…"
          spellCheck={false}
        />
        <button
          className="mem-sweep"
          onClick={onSweep}
          disabled={busy || !online}
          title="Run decay now"
        >
          sweep
        </button>
      </div>
      {note ? <p className="mem-note">{note}</p> : null}
      {picks.length ? (
        <div className="mem-picks">
          <span className="mem-picked">{picks.length} picked</span>
          <button
            className="mem-pick-act"
            onClick={onMergePicked}
            disabled={picks.length < 2 || busy}
            title="Fold the picked memories into one"
          >
            merge
          </button>
          <button
            className="mem-pick-act"
            onClick={onForgetPicked}
            disabled={busy}
            title="Forget the picked memories"
          >
            forget
          </button>
          <button
            className="mem-pick-act"
            onClick={onClearPicks}
            title="Clear the selection"
          >
            clear
          </button>
        </div>
      ) : null}
      <div className="mem-list">
        {memories.length === 0 ? (
          <p className="empty">
            {busy ? 'reading the shard…' : 'Nothing in the shard yet.'}
          </p>
        ) : null}
        {memories.map((item) => (
          <div
            key={item.id}
            className="mem"
            data-kind={item.kind}
            data-picked={picks.includes(item.id) ? 'true' : undefined}
          >
            <div className="mem-head">
              <span className="mem-kind">{item.kind}</span>
              <span className="mem-id">#{item.id}</span>
              <span className="spacer" />
              {item.sources?.length ? (
                <span className="mem-src">{item.sources.join('+')}</span>
              ) : null}
              <span
                className="mem-imp"
                title={`importance ${item.importance.toFixed(2)}`}
              >
                {Math.round(item.importance * 100)}
              </span>
              <button
                className="mem-pick"
                aria-pressed={picks.includes(item.id)}
                title={
                  picks.includes(item.id) ? 'Unpick' : 'Pick to merge or forget'
                }
                onClick={() => onTogglePick(item.id)}
              >
                {picks.includes(item.id) ? '◼' : '◻'}
              </button>
              <button
                className="mem-forget"
                title="Forget this"
                onClick={() => onForget(item.id)}
              >
                ×
              </button>
            </div>
            <p className="mem-text">{item.text}</p>
          </div>
        ))}
      </div>
    </div>
  )
}
