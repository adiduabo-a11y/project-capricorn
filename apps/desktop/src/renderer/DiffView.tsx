/** A readable unified diff. No editor, no syntax tree: enough to review a hunk. */

interface DiffViewProps {
  text: string
  path?: string
}

export function DiffView({ text, path }: DiffViewProps) {
  const lines = text.split('\n')
  return (
    <div className="diff" role="region" aria-label={path ? `diff for ${path}` : 'diff'}>
      <pre className="diff-body">
        {lines.map((line, index) => {
          const kind = line.startsWith('+++') || line.startsWith('---')
            ? 'meta'
            : line.startsWith('@@')
              ? 'hunk'
              : line.startsWith('+')
                ? 'add'
                : line.startsWith('-')
                  ? 'del'
                  : 'ctx'
          return (
            <div key={index} className="diff-line" data-kind={kind}>
              {line || ' '}
            </div>
          )
        })}
      </pre>
    </div>
  )
}
