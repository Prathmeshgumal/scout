import type { GoalEvent, GoalStatus } from "./api"
import type { StepState } from "./chat-state"

/** One tool call. runId: the subagent run that made it. Null for a step of the main agent. */
export type Step = {
  id: string
  tool: string
  label: string
  agent: string
  runId: string | null
  state: StepState
  summary?: string
  saved: boolean
  startedAt: number
  endedAt: number | null
}

export type Run = { id: string; agent: string; task: string; state: StepState; summary?: string; startedAt: number; endedAt: number | null; steps: Step[] }

export type Item = { kind: "run"; run: Run } | { kind: "step"; step: Step }

/** The work for one message of the user. n starts at 1. text: the first line of the message. */
export type Answer = { n: number; text: string; at: number; items: Item[] }

/** steps: all steps in the order they started. agents: one lane each, in the order they first worked. */
export type Activity = { answers: Answer[]; steps: Step[]; agents: string[]; start: number; end: number }

export type Filter = { query: string; kind: "all" | "problems" | "saves" }

const str = (value: unknown) => (typeof value === "string" ? value : "")

export function buildActivity(events: GoalEvent[], status: GoalStatus, now: number): Activity {
  const answers: Answer[] = []
  const steps: Step[] = []
  const stepById = new Map<string, Step>()
  const runById = new Map<string, Run>()
  const agents: string[] = []
  const seen = (agent: string) => {
    if (agent && !agents.includes(agent)) agents.push(agent)
  }
  let time = Number.NaN
  let first = Number.NaN

  const answer = (): Answer => {
    if (!answers.length) answers.push({ n: 1, text: "", at: time, items: [] })
    return answers[answers.length - 1]
  }

  for (const event of events) {
    const parsed = event.at ? Date.parse(event.at) : Number.NaN
    if (!Number.isNaN(parsed)) time = parsed
    if (Number.isNaN(first) && !Number.isNaN(time)) first = time
    switch (event.type) {
      case "user_message":
        answers.push({ n: answers.length + 1, text: str(event.text).split("\n")[0].trim(), at: time, items: [] })
        break
      case "agent_started": {
        const run: Run = { id: str(event.tool_use_id), agent: str(event.name), task: str(event.task), state: "running", startedAt: time, endedAt: null, steps: [] }
        runById.set(run.id, run)
        seen(run.agent)
        answer().items.push({ kind: "run", run })
        break
      }
      case "agent_finished": {
        const run = runById.get(str(event.tool_use_id))
        if (!run) break
        run.state = event.ok ? "done" : event.status === "stopped" || event.status === "killed" ? "stopped" : "failed"
        run.summary = str(event.summary) || undefined
        run.endedAt = time
        break
      }
      case "tool_started": {
        const callId = str(event.call_id)
        const run = callId ? runById.get(callId) : undefined
        const step: Step = {
          id: str(event.tool_use_id),
          tool: str(event.tool),
          label: str(event.label) || str(event.tool),
          agent: str(event.agent) || run?.agent || "main",
          runId: run ? run.id : null,
          state: "running",
          saved: false,
          startedAt: time,
          endedAt: null,
        }
        stepById.set(step.id, step)
        steps.push(step)
        seen(step.agent)
        if (run) run.steps.push(step)
        else answer().items.push({ kind: "step", step })
        break
      }
      case "tool_finished": {
        const step = stepById.get(str(event.tool_use_id))
        if (!step) break
        step.state = event.ok ? "done" : "failed"
        step.summary = str(event.summary) || undefined
        step.saved = Boolean(event.saved)
        step.endedAt = time
        break
      }
    }
  }

  if (status !== "running") {
    for (const step of steps) if (step.state === "running") step.state = "stopped"
    for (const run of runById.values()) if (run.state === "running") run.state = "stopped"
  }
  const start = Number.isNaN(first) ? now : first
  const end = status === "running" ? Math.max(now, start) : Number.isNaN(time) ? start : time
  return { answers, steps, agents, start, end }
}

export function matches(step: Step, filter: Filter): boolean {
  if (filter.kind === "problems" && step.state !== "failed") return false
  if (filter.kind === "saves" && !step.saved) return false
  const query = filter.query.trim().toLowerCase()
  if (!query) return true
  return [step.label, step.tool, step.agent, step.summary ?? ""].some((text) => text.toLowerCase().includes(query))
}

/** The id of the step before (-1) or after (1) this one, in the order they started. */
export function nextStep(steps: Step[], id: string, direction: 1 | -1): string | null {
  const index = steps.findIndex((s) => s.id === id)
  if (index < 0) return null
  return steps[index + direction]?.id ?? null
}

/** The step to show first: the first one that failed, else the last one. */
export function firstStep(steps: Step[]): string | null {
  return (steps.find((s) => s.state === "failed") ?? steps.at(-1))?.id ?? null
}
