import { useCallback, useEffect, useState } from "react"
import { Button } from "@/components/ui/button"
import { listJobs, type JobCardData } from "@/lib/api"
import { JobGrid } from "./JobGrid"
import { SkeletonCards } from "./JobCard"
import { JobPanel } from "./JobPanel"

const NOT_WORKING = { research: false, check: false }

/** The roles of all goals. */
export function AllJobs({ onNewChat }: { onNewChat: () => void }) {
  const [jobs, setJobs] = useState<JobCardData[] | null>(null)
  const [failed, setFailed] = useState(false)
  const [open, setOpen] = useState<JobCardData | null>(null)
  const close = useCallback(() => setOpen(null), [])
  useEffect(() => {
    let active = true
    const read = () => listJobs().then((list) => { if (active) { setJobs(list); setFailed(false) } }).catch(() => { if (active) setFailed(true) })
    void read()
    const timer = setInterval(() => void read(), 10_000)
    return () => { active = false; clearInterval(timer) }
  }, [])
  return (
    <div className="flex min-h-0 flex-1">
      <div className="min-w-0 flex-1 overflow-y-auto">
        <div className="mx-auto flex max-w-[1240px] flex-col gap-4 p-4">
          <h1 className="text-[17px] font-semibold">Roles from all goals</h1>
          {failed && !jobs && <p className="text-red-700 dark:text-red-300">Could not read the roles. The page tries again in 10 seconds.</p>}
          {!jobs && !failed && <SkeletonCards />}
          {jobs?.length === 0 && (
            <div className="flex flex-col items-start gap-2 py-6">
              <p className="font-medium">No roles yet.</p>
              <p className="text-muted-foreground">Start a chat and say what you want. The roles of each goal appear here.</p>
              <Button size="sm" onClick={onNewChat}>Start a chat</Button>
            </div>
          )}
          {jobs && jobs.length > 0 && <JobGrid jobs={jobs} selectedId={open?.id} onOpen={setOpen} working={NOT_WORKING} showGoal />}
        </div>
      </div>
      {open && <JobPanel key={open.id} job={open} onClose={close} />}
    </div>
  )
}
