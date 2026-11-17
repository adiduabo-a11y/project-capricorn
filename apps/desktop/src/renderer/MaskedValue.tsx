import { useState } from 'react'

/**
 * An environment value shown only when it is asked for.
 *
 * A server's env holds tokens, and the settings row and the approval card are
 * both on screen during calls and screen shares. The mask is fixed rather than
 * one mark per character, so it does not leak the length either. An empty value
 * holds nothing to hide and says so.
 */
export function MaskedValue({ text }: { text: string }) {
  const [shown, setShown] = useState(false)
  if (text === '') return <span>(empty)</span>
  return (
    <button
      className="masked-value"
      title={shown ? 'hide this value' : 'show this value'}
      onClick={() => setShown(!shown)}
    >
      {shown ? text : '••••••'}
    </button>
  )
}
