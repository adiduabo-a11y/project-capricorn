/**
 * Which requests actually ask Capri to move.
 *
 * The bar is high on purpose. A message that names an object ("hide the
 * toolbar", "move left") is a request about the app, and she has no business
 * walking off in the middle of it. Only a bare, addressed instruction counts:
 * "Capri, come here", "go away", "jump". Politeness is stripped; content is not.
 */

import type { PetCue } from './bridge'

const DIRECTIVES: Array<[PetCue, RegExp]> = [
  ['come', /^(?:come (?:here|over|to me|back)|over here|get over here|closer)$/],
  ['away', /^(?:go away|shoo|scram|get lost|hide|leave me(?: alone)?)$/],
  ['blink', /^(?:teleport|blink)(?: (?:away|somewhere|anywhere))?$/],
  ['hop', /^(?:jump|hop|bounce)(?: (?:up|around|again))?$/],
  ['dance', /^dance$/],
  ['left', /^go (?:to the |the )?left$/],
  ['right', /^go (?:to the |the )?right$/],
  ['settle', /^(?:sit still|stay put|stay there|stop moving|settle down|calm down)$/],
]

// A trailing courtesy never changes the instruction.
const TAIL = /\s+(?:please|now|again|right now|for me|for a second|for a moment|for a minute|a second|a moment|a minute)$/

export function petCueFor(text: string): PetCue | null {
  let line = text
    .toLowerCase()
    .replace(/[.!?]+$/, '')
    .replace(/^\s*(?:hey|ok|okay)?[,\s]*capri[\s,:–—-]+/, '')
    .replace(/[\s,]+capri$/, '')
    .trim()
  if (!line) return null

  for (let guard = 0; guard < 3; guard += 1) {
    const shorter = line.replace(TAIL, '')
    if (shorter === line) break
    line = shorter
  }
  line = line.replace(/^please\s+/, '')

  // Anything longer than a short instruction is a sentence, and a sentence
  // about the work is not an instruction to her.
  if (!line || line.split(/\s+/).length > 4) return null

  for (const [cue, pattern] of DIRECTIVES) {
    if (pattern.test(line)) return cue
  }
  return null
}
