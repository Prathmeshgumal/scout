import { MessageSquare } from "lucide-react"
import { Button } from "@/components/ui/button"
import type { GoalSummary, Mandate } from "@/lib/api"

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
const STATUS = { active: "Active", paused: "Paused", ended: "Ended" }

export function scheduleText(m: Mandate): string {
  const unit = m.every_count === 1 ? `Each ${m.every_unit}` : `Every ${m.every_count} ${m.every_unit}s`
  const days = m.window_days.length === 7 ? "" : ` on ${m.window_days.map((d) => DAYS[d - 1]).join(", ")}`
  return `${unit}${days} from ${m.window_start.slice(0, 5)} (${m.timezone})`
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return <div className="contents"><dt className="text-muted-foreground">{label}</dt><dd className="min-w-0 break-words tabular-nums">{children}</dd></div>
}

export function MandateView({ mandate, goals, onOpenGoal }: { mandate: Mandate; goals: GoalSummary[]; onOpenGoal: (id: string) => void }) {
  const runs = goals.filter((g) => g.mandate_id === mandate.id)
  const fit = runs.reduce((sum, g) => sum + g.fit, 0)
  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex max-w-[720px] flex-col gap-4 p-4">
        <div>
          <h1 className="text-[17px] leading-snug font-semibold break-words">{mandate.title}</h1>
          <p className="mt-1 leading-relaxed break-words text-muted-foreground">{mandate.instruction}</p>
        </div>
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1.5 rounded-xl border bg-card p-3.5">
          <Fact label="State">{STATUS[mandate.status]}</Fact>
          <Fact label="Schedule">{scheduleText(mandate)}</Fact>
          <Fact label="Roles in each run">{mandate.jobs_per_run}</Fact>
          <Fact label="Spend limit of a run">${Number(mandate.max_spend_per_run).toFixed(2)}</Fact>
          {mandate.next_run_at && <Fact label="Next run">{new Date(mandate.next_run_at).toLocaleString()}</Fact>}
          <Fact label="So far">{runs.length} run{runs.length === 1 ? "" : "s"}, {fit} fit role{fit === 1 ? "" : "s"}</Fact>
        </dl>
        <div><Button variant="outline" size="sm" onClick={() => onOpenGoal(mandate.home_goal_id)}><MessageSquare aria-hidden /> Change it in the chat</Button></div>
      </div>
    </div>
  )
}
