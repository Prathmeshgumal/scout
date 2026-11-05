/** The most buttons that a reply can show. */
export const MAX_CHOICES = 4

/** danger: the choice cannot be undone, e.g. "End mandate". The line marks it with "!" before its label. */
export type Choice = { label: string; danger: boolean }

// The last line of a reply, e.g. "[[Start mandate | Change settings]]". The agent prompts write it.
const MARKER = /(?:^|\n)[ \t]*\[\[([^\]\n]*)(\]\])?\s*$/

/** The text of a reply without its last line of choices, and the choices. A marker that is still
 * being written is hidden, with no choices. */
export function splitChoices(text: string): { text: string; choices: Choice[] } {
  const match = MARKER.exec(text)
  if (!match) return { text, choices: [] }
  const choices = match[2]
    ? match[1].split("|").map(toChoice).filter((c) => c.label).slice(0, MAX_CHOICES)
    : []
  return { text: text.slice(0, match.index).trimEnd(), choices }
}

function toChoice(part: string): Choice {
  const label = part.trim()
  return label.startsWith("!") ? { label: label.slice(1).trim(), danger: true } : { label, danger: false }
}
