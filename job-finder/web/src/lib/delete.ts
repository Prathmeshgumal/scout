import type { DeletePreview } from "./api"

const count = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`

/** The text of the delete dialog: what goes and what stays. */
export function deleteText(kind: "goal" | "mandate", p: DeletePreview): string {
  const data = `${count(p.jobs, "job")} and ${count(p.notes, "application note")}`
  const parts = [kind === "mandate"
    ? `This deletes the mandate, its chat, its ${count(p.goals, "goal")}, ${data}.`
    : `This deletes the goal, its chat, ${data}.`]
  if (p.jobs > 0) parts.push("These jobs leave the Jobs page. Jobs in the Job pipeline stay, with their notes.")
  if (p.working) parts.push("The agents that work on it stop first.")
  parts.push("You cannot undo this.")
  return parts.join(" ")
}
