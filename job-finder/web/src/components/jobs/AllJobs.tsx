import { ChevronRight } from "lucide-react"
import { useCallback, useEffect, useMemo, useState } from "react"
import { Button } from "@/components/ui/button"
import { listJobs, type JobCardData } from "@/lib/api"
import { groupByGoal, groupSummary } from "@/lib/jobs"
import { cn } from "@/lib/utils"
import { JobGrid } from "./JobGrid"
import { SkeletonRows } from "./JobRow"
import { JobPanel } from "./JobPanel"

const NOT_WORKING = { research: false, check: false }

/** The roles goal by goal. A button shows the roles of all goals at once. */
export function AllJobs({ onNewChat, onOpenGoal }: { onNewChat: () => void; onOpenGoal: (id: string) => void }) {
  const [jobs, setJobs] = useState<JobCardData[] | null>(null)
  const [failed, setFailed] = useState(false)
  const [open, setOpen] = useState<JobCardData | null>(null)
  const [showAll, setShowAll] = useState(false)
  const [openGoals, setOpenGoals] = useState<Set<string>>(new Set())
  const close = useCallback(() => setOpen(null), [])
  useEffect(() => {
    let active = true
    const read = () => listJobs().then((list) => { if (active) { setJobs(list); setFailed(false) } }).catch(() => { if (active) setFailed(true) })
    void read()
    const timer = setInterval(() => void read(), 10_000)
    return () => { active = false; clearInterval(timer) }
  }, [])
  const groups = useMemo(() => groupByGoal(jobs ?? []), [jobs])
  const toggle = (id: string) => setOpenGoals((now) => {
    const next = new Set(now)
    if (!next.delete(id)) next.add(id)
    return next
  })
  return (
    <div className="flex min-h-0 flex-1">
      <div className="min-w-0 flex-1 overflow-y-auto">
        <div className="mx-auto flex max-w-[1040px] flex-col gap-4 p-4">
          <header className="flex items-center justify-between gap-3">
            <h1 className="text-[17px] font-semibold">{showAll ? "All jobs" : "Jobs by goal"}</h1>
            {jobs && jobs.length > 0 && (
              <Button variant="outline" size="sm" onClick={() => setShowAll(!showAll)}>{showAll ? "Show jobs by goal" : "Show all jobs"}</Button>
            )}
          </header>
          {failed && !jobs && <p className="text-red-700 dark:text-red-300">Could not read the roles. The page tries again in 10 seconds.</p>}
          {!jobs && !failed && <SkeletonRows />}
          {jobs?.length === 0 && (
            <div className="flex flex-col items-start gap-2 py-6">
              <p className="font-medium">No roles yet.</p>
              <p className="text-muted-foreground">Start a chat and say what you want. The roles of each goal appear here.</p>
              <Button size="sm" onClick={onNewChat}>Start a chat</Button>
            </div>
          )}
          {jobs && jobs.length > 0 && showAll && <JobGrid jobs={jobs} selectedId={open?.id} onOpen={setOpen} working={NOT_WORKING} showGoal />}
          {jobs && jobs.length > 0 && !showAll && (
            <ul className="flex flex-col gap-2">
              {groups.map((group) => {
                const expanded = openGoals.has(group.goalId)
                return (
                  <li key={group.goalId} className="rounded-xl border bg-card">
                    <div className="flex items-center gap-1 pr-2">
                      <button type="button" aria-expanded={expanded} onClick={() => toggle(group.goalId)}
                        className="flex min-w-0 flex-1 items-center gap-2 rounded-xl px-3 py-2.5 text-left outline-none focus-visible:ring-2 focus-visible:ring-violet-500/50">
                        <ChevronRight className={cn("size-4 shrink-0 text-muted-foreground transition-transform", expanded && "rotate-90")} aria-hidden />
                        <span className="min-w-0">
                          <span className="block truncate font-medium">{group.title}</span>
                          <span className="block truncate text-[12.5px] text-muted-foreground">{groupSummary(group)}</span>
                        </span>
                      </button>
                      <Button variant="ghost" size="sm" onClick={() => onOpenGoal(group.goalId)}>Open goal</Button>
                    </div>
                    {expanded && (
                      <div className="border-t p-3">
                        <JobGrid jobs={group.jobs} selectedId={open?.id} onOpen={setOpen} working={NOT_WORKING} />
                      </div>
                    )}
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      </div>
      {open && <JobPanel key={open.id} job={open} onClose={close} />}
    </div>
  )
}
