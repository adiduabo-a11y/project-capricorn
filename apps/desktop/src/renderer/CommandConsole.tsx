/** A command transcript: what ran, what it printed, what it exited with. */

interface CommandConsoleProps {
  text: string
  title?: string
}

export function CommandConsole({ text, title }: CommandConsoleProps) {
  return (
    <div className="console">
      {title ? <div className="console-head">{title}</div> : null}
      <pre className="console-out">{text || '(no output)'}</pre>
    </div>
  )
}
