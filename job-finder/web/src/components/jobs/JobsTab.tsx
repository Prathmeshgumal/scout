import { RotateCcw } from "lucide-react"
import { useState } from "react"
import { Button } from "@/components/ui/button"
import type { JobCardData, Report } from "@/lib/api"
import { JobGrid } from "./JobGrid"
import { SkeletonCards } from "./JobCard"
import { JobPanel } from "./JobPanel"
import { ProgressHeader } from "./ProgressHeader"

/** What a goal with no roles to show says. */
export function EmptyRoles({ report, onRetry, onChat }: { report: Report; onRetry: () => void; onChat: () => void }) {
  const { status, status_reason: reason } = report.goal
  if (status === "running" || status === "waiting") {
    return (
      <div className="flex flex-col gap-3">
        <p className="text-muted-foreground">No roles saved yet. Cards appear here when the agents save them.</p>
        <SkeletonCards count={3} />
      </div>
    )
  }
  if (status === "failed" || status === "stopped" || status === "capped") {
    return (
      <div className="flex flex-col items-start gap-2 py-6">
        <p className="font-medium">{status === "failed" ? "This goal failed before it saved a role." : status === "stopped" ? "You stopped this goal before it saved a role." : "This goal reached its limit before it saved a role."}</p>
        {reason && <p className="text-muted-foreground break-words">{reason}</p>}
        {status !== "capped" && <Button size="sm" variant="outline" onClick={onRetry}><RotateCcw aria-hidden /> Retry</Button>}
      </div>
    )
  }
  return <NoFit report={report} onChat={onChat} />
}

/** A finished goal with no fit role: what it searched, and what to change. */
export function NoFit({ report, onChat }: { report: Report; onChat: () => void }) {
  return (
    <div className="flex flex-col items-start gap-2 rounded-xl border bg-card p-4">
      <p className="font-medium">No role fit this search.</p>
      {report.searches.length > 0 && (
        <div className="min-w-0">
          <p className="text-muted-foreground">The agents searched for:</p>
          <ul className="mt-1 list-disc pl-5 break-words">{report.searches.slice(0, 6).map((q) => <li key={q}>{q}</li>)}</ul>
        </div>
      )}
      <p className="text-muted-foreground">Try a wider place, more titles or fewer must-haves. Tell the agent in the chat.</p>
      <Button size="sm" variant="outline" onClick={onChat}>Open the chat</Button>
    </div>
  )
}

export function JobsTab({ report, tasks, onRetry, onChat }: { report: Report | null; tasks: string[]; onRetry: () => void; onChat: () => void }) {
  const [open, setOpen] = useState<JobCardData | null>(null)
  if (!report) return <div className="min-h-0 flex-1 overflow-y-auto p-4"><SkeletonCards /></div>
  const working = report.goal.status === "running"
  const finishedNoFit = report.goal.status === "done" && report.jobs.length > 0 && report.funnel.fit === 0
  return (
    <div className="flex min-h-0 flex-1">
      <div className="min-w-0 flex-1 overflow-y-auto">
        <div className="mx-auto flex max-w-[1240px] flex-col gap-4 p-4">
          <ProgressHeader report={report} />
          {finishedNoFit && <NoFit report={report} onChat={onChat} />}
          {report.jobs.length ? (
            <JobGrid jobs={report.jobs} selectedId={open?.id} onOpen={setOpen}
              working={{ research: working && tasks.includes("job-research"), check: working && tasks.includes("fit-check") }} />
          ) : <EmptyRoles report={report} onRetry={onRetry} onChat={onChat} />}
        </div>
      </div>
      {open && <JobPanel key={open.id} job={open} onClose={() => setOpen(null)} />}
    </div>
  )
}
