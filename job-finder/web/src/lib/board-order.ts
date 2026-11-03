/** The place of a card in a column of a board. Place 0 is at the top: the highest priority. */
export type Positions = Record<string, { column: string; position: number }>
export type Columns = Record<string, string[]>

/**
 * The rows of a column in the order that the user chose. A row with no place in this column comes first, in the order
 * of the server: it is new, or it came from another column with no drag. The rows with a place follow, top first.
 */
export function inOrder<T>(rows: T[], keyOf: (row: T) => string, column: string, positions: Positions): T[] {
  const place = (row: T) => {
    const found = positions[keyOf(row)]
    return found && found.column === column ? found.position : null
  }
  const free = rows.filter((row) => place(row) === null)
  const placed = rows.filter((row) => place(row) !== null).sort((a, b) => (place(a) ?? 0) - (place(b) ?? 0))
  return [...free, ...placed]
}

/** Take a card from its column and put it in a column at an index. */
export function moveCard(columns: Columns, key: string, to: string, index: number): Columns {
  const next: Columns = Object.fromEntries(Object.entries(columns).map(([id, list]) => [id, list.filter((k) => k !== key)]))
  const target = next[to] ?? []
  next[to] = [...target.slice(0, index), key, ...target.slice(index)]
  return next
}

/**
 * The full order of a column after the user moved the cards that a filter shows. The hidden cards keep their places;
 * the shown cards take the places of the shown cards, in their new order. A shown card with no place goes at the end.
 */
export function placeVisible(full: string[], visible: string[]): string[] {
  const shown = new Set(visible)
  const queue = [...visible]
  const out = full.map((key) => (shown.has(key) ? queue.shift()! : key))
  return [...out, ...queue]
}
