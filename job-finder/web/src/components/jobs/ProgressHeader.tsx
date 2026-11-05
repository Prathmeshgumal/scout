import { AlertTriangle, Download, Hourglass } from "lucide-react"
import type { ReactNode } from "react"
import { GlowBorder } from "@/components/glow/GlowBorder"
import { Button } from "@/components/ui/button"
import { toast } from "sonner"
import { download, reportMarkdownUrl, type GoalStatus, type Report } from "@/lib/api"
import { span } from "@/lib/jobs"
import { cn } from "@/lib/utils"

export const STATUS_LABEL: Record<GoalStatus, string> = {
  running: "Running", waiting: "Waiting", done: "Finished", capped: "Limit reached", failed: "Failed", stopped: "Stopped",
}

const STATUS_TONE: Record<GoalStatus, string> = {
  running: "text-violet-700 dark:text-violet-300", waiting: "text-amber-700 dark:text-amber-300",
  done: "text-emerald-700 dark:text-emerald-300", capped: "text-amber-700 dark:text-amber-300",
  failed: "text-red-700 dark:text-red-300", stopped: "text-muted-foreground",
}

function Count({ value, label, strong }: { value: number; label: string; strong?: boolean }) {
  return (
    <span className="whitespace-nowrap">
      <b className={cn("font-semibold tabular-nums", strong ? "text-emerald-700 dark:text-emerald-300" : "text-foreground")}>{value}</b>{" "}
      <span className="text-muted-foreground">{label}</span>
    </span>
  )
}

export function ProgressHeader({ report }: { report: Report }) {
  const { goal, funnel, timing, warnings, waits } = report
  const running = goal.status === "running"
  const target = report.pipeline.target
  const left = timing.limit_s ? timing.limit_s - timing.work_seconds : null
  const reasons = waits.flatMap((w) => (w.reason ? [w.reason] : []))
  const line = report.now || (goal.status !== "done" ? goal.status_reason : "") || ""
  const body: ReactNode = (
    <div className="flex flex-col gap-3 p-4">
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div className="min-w-0">
          <h1 className="text-[17px] leading-snug font-semibold break-words">{report.title}</h1>
          <p className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[12px] text-muted-foreground">
            <span className={cn("font-medium", STATUS_TONE[goal.status])}>{STATUS_LABEL[goal.status]}</span>
            {left !== null && <span>{left > 0 ? `${span(left)} left` : "Time limit reached"}</span>}
            <span className="tabular-nums">${goal.spend_usd.toFixed(2)} of ${goal.max_spend_usd.toFixed(2)}</span>
            {target ? <span className="tabular-nums">{funnel.fit} of {target} fit roles</span> : null}
          </p>
        </div>
        <Button variant="outline" size="sm" className="text-[12px]" onClick={() => {
          download(reportMarkdownUrl(goal.id)).catch((err: unknown) => toast.error(err instanceof Error ? err.message : "The download failed."))
        }}>
          <Download aria-hidden /> Download report
        </Button>
      </div>
      <div className="flex flex-wrap gap-x-5 gap-y-1">
        <Count value={funnel.found} label="found" />
        <Count value={funnel.researched} label="researched" />
        <Count value={funnel.checked} label="checked" />
        <Count value={funnel.fit} label="fit" strong />
      </div>
      {target ? (
        <div className="h-[3px] overflow-hidden rounded-full bg-muted" role="progressbar" aria-label="Fit roles" aria-valuenow={funnel.fit} aria-valuemin={0} aria-valuemax={target}>
          <div className="h-full rounded-full bg-emerald-500 transition-[width] duration-500" style={{ width: `${Math.min(100, (funnel.fit / target) * 100)}%` }} />
        </div>
      ) : null}
      {line && <p className="text-muted-foreground" aria-live="polite">{line}</p>}
      {reasons.length > 0 && !line.startsWith("Waiting") && (
        <p className="flex items-center gap-1.5 text-amber-700 dark:text-amber-300"><Hourglass className="size-3.5 shrink-0" aria-hidden /> {reasons[0]}</p>
      )}
      {warnings.map((warning) => (
        <p key={warning.text} className="flex items-start gap-1.5 rounded-lg bg-amber-500/10 px-2.5 py-1.5 text-amber-800 dark:text-amber-200">
          <AlertTriangle className="mt-[2px] size-3.5 shrink-0" aria-hidden /> <span className="min-w-0 break-words">{warning.text}</span>
        </p>
      ))}
    </div>
  )
  return running ? <GlowBorder>{body}</GlowBorder> : <div className="rounded-xl border bg-card">{body}</div>
}
