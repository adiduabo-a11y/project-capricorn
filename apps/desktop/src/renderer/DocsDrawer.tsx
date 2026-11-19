import type { CollectionInfo, RagHit } from './bridge'

export function DocsDrawer({
  online,
  path,
  onPathChange,
  query,
  onQueryChange,
  busy,
  note,
  collections,
  hits,
  shortPath,
  onIngest,
  onOpenDoc,
}: {
  online: boolean
  path: string
  onPathChange: (value: string) => void
  query: string
  onQueryChange: (value: string) => void
  busy: boolean
  note: string
  collections: CollectionInfo[]
  hits: RagHit[]
  shortPath: (path: string) => string
  onIngest: () => void
  onOpenDoc: (path: string, offset: number) => void
}) {
  return (
    <div className="docs">
      <div className="mem-search">
        <input
          value={path}
          onChange={(event) => onPathChange(event.target.value)}
          placeholder="folder or file to index…"
          spellCheck={false}
        />
        <button
          className="mem-sweep"
          onClick={onIngest}
          disabled={busy || !online || !path.trim()}
          title="Index this path"
        >
          index
        </button>
      </div>
      {note ? <p className="mem-note">{note}</p> : null}
      <div className="doc-cols">
        {collections.length === 0 ? (
          <span className="empty">Nothing indexed yet.</span>
        ) : (
          collections.map((collection) => (
            <span key={collection.name} className="doc-col">
              {collection.name}
              <b>{collection.docs}</b>docs <b>{collection.chunks}</b>chunks
            </span>
          ))
        )}
      </div>
      <div className="mem-search">
        <input
          value={query}
          onChange={(event) => onQueryChange(event.target.value)}
          placeholder="search documents…"
          spellCheck={false}
        />
      </div>
      <div className="mem-list">
        {hits.length === 0 ? (
          <p className="empty">{busy ? 'searching…' : 'No matches.'}</p>
        ) : null}
        {hits.map((hit) => (
          <div
            key={hit.chunkId}
            className="doc"
            role="button"
            tabIndex={0}
            onClick={() => onOpenDoc(hit.path, hit.span[0])}
            onKeyDown={(event) => {
              if (event.key === 'Enter') onOpenDoc(hit.path, hit.span[0])
            }}
          >
            <div className="mem-head">
              <span className="doc-path">{shortPath(hit.path)}</span>
              {hit.heading ? <span className="doc-heading">› {hit.heading}</span> : null}
              <span className="spacer" />
              <span className="mem-imp">{Math.round(hit.score * 100)}</span>
            </div>
            <p className="mem-text">{hit.text.slice(0, 220)}</p>
          </div>
        ))}
      </div>
    </div>
  )
}
