import { ArrowUpRight } from "lucide-react"
import { AgentDot, StateIcon, STATE_WORD } from "@/components/activity/agents"
import type { GoalStatus } from "@/lib/api"
import { shownState, type AgentLine } from "@/lib/chat-state"
import { cn } from "@/lib/utils"

const SHOWN = 3

/** A subagent run in the chat: its result and its last steps. onActivity: opens a step in the Activity tab. */
export function AgentCard({ line, status, onActivity }: { line: AgentLine; status: GoalStatus; onActivity?: (stepId: string | null) => void }) {
  const state = shownState(line, status)
  const last = line.steps.slice(-SHOWN)
  const earlier = line.steps.length - last.length
  return (
    <div className="flex flex-col gap-1.5 rounded-lg border bg-card/60 px-3 py-2 text-[12px]">
      <p className="flex min-w-0 items-center gap-2">
        <StateIcon state={state} />
        <AgentDot agent={line.name} />
        <span className="font-medium text-foreground">{line.name}</span>
        <span className="text-muted-foreground">{STATE_WORD[state]}</span>
      </p>
      {(line.summary || line.task) && <p className="min-w-0 break-words text-muted-foreground">{line.summary || line.task}</p>}
      {last.length > 0 && (
        <ol className="flex flex-col gap-0.5 border-l pl-2.5" aria-label={`Last steps of ${line.name}`}>
          {last.map((step) => {
            const stepState = step.state === "running" && status !== "running" ? "stopped" : step.state
            return (
              <li key={step.id} className="flex min-w-0 items-center gap-1.5">
                <StateIcon state={stepState} className="size-3" />
                <span className={cn("min-w-0 truncate", stepState === "failed" ? "text-red-700 dark:text-red-300" : "text-foreground/80")}>{step.label}</span>
                {step.summary && <span className="min-w-0 shrink truncate text-muted-foreground">{step.summary}</span>}
              </li>
            )
          })}
        </ol>
      )}
      {(earlier > 0 || onActivity) && (
        <div className="flex items-center gap-3 text-muted-foreground">
          {earlier > 0 && <span>{earlier} earlier {earlier === 1 ? "step" : "steps"}</span>}
          {onActivity && (
            <button type="button" onClick={() => onActivity(line.steps.at(-1)?.id ?? null)}
              className="ml-auto inline-flex items-center gap-0.5 rounded outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring">
              Open in Activity <ArrowUpRight aria-hidden className="size-3" />
            </button>
          )}
        </div>
      )}
    </div>
  )
}
