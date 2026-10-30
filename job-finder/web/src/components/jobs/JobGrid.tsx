import { ChevronRight, Search } from "lucide-react"
import { useMemo, useState } from "react"
import { Input } from "@/components/ui/input"
import { Select } from "@/components/ui/select"
import type { JobCardData, Verdict } from "@/lib/api"
import { NO_FILTERS, VERDICT_LABEL, filterJobs, sortJobs, workplaces, type JobFilters } from "@/lib/jobs"
import { cn } from "@/lib/utils"
import { JobCard } from "./JobCard"

const VERDICTS: (Verdict | "all" | "progress")[] = ["all", "fit", "partial", "unsure", "progress", "not_fit"]
const PILL_LABEL = (v: (typeof VERDICTS)[number]) => (v === "all" ? "All" : v === "progress" ? "In progress" : VERDICT_LABEL[v])
const GRID = "grid grid-cols-[repeat(auto-fill,minmax(min(100%,300px),1fr))] gap-3"

type Props = {
  jobs: JobCardData[]
  working: { research: boolean; check: boolean }
  selectedId?: string | null
  onOpen: (job: JobCardData) => void
  /** Show the goal of each role: on the page of all roles. */
  showGoal?: boolean
}

export function JobGrid({ jobs, working, selectedId, onOpen, showGoal }: Props) {
  const [filters, setFilters] = useState<JobFilters>(NO_FILTERS)
  const [notFitOpen, setNotFitOpen] = useState(false)
  const shown = useMemo(() => sortJobs(filterJobs(jobs, filters)), [jobs, filters])
  const count = (v: (typeof VERDICTS)[number]) =>
    filterJobs(jobs, { ...filters, verdict: v }).length
  const places = workplaces(jobs)
  const main = shown.filter((job) => job.fit !== "not_fit" || filters.verdict === "not_fit")
  const notFit = filters.verdict === "not_fit" ? [] : shown.filter((job) => job.fit === "not_fit")
  const card = (job: JobCardData) => (
    <JobCard key={job.id} job={job} working={working} selected={job.id === selectedId} onOpen={onOpen}
      goalTitle={showGoal ? job.goal_title : undefined} />
  )
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <div className="flex flex-wrap gap-1" role="group" aria-label="Filter by verdict">
          {VERDICTS.map((v) => (
            <button key={v} type="button" aria-pressed={filters.verdict === v}
              onClick={() => setFilters({ ...filters, verdict: v })}
              className={cn("h-7 rounded-md px-2.5 text-[12px] whitespace-nowrap transition-colors",
                filters.verdict === v ? "bg-violet-500/15 text-violet-700 ring-1 ring-violet-500/30 ring-inset dark:text-violet-300"
                  : "text-muted-foreground hover:bg-muted hover:text-foreground")}>
              {PILL_LABEL(v)} <span className="tabular-nums opacity-70">{count(v)}</span>
            </button>
          ))}
        </div>
        {places.length > 1 && (
          <Select label="Filter by workplace" value={filters.workplace} onChange={(workplace) => setFilters({ ...filters, workplace })}
            className="h-7 rounded-md bg-card text-[12px] capitalize"
            choices={[{ value: "all", label: "Any workplace" }, ...places.map((p) => ({ value: p, label: p[0].toUpperCase() + p.slice(1) }))]} />
        )}
        <div className="relative min-w-[160px] flex-1 sm:max-w-[260px]">
          <Search className="absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-muted-foreground" aria-hidden />
          <Input aria-label="Search roles" placeholder="Search roles" value={filters.text}
            onChange={(e) => setFilters({ ...filters, text: e.target.value })} className="h-7 pl-7 text-[12px] md:text-[12px]" />
        </div>
      </div>
      {main.length > 0 ? <div className={GRID}>{main.map(card)}</div> : shown.length === 0 && (
        <p className="py-8 text-center text-muted-foreground">No roles match these filters.</p>
      )}
      {notFit.length > 0 && (
        <section>
          <button type="button" aria-expanded={notFitOpen} onClick={() => setNotFitOpen(!notFitOpen)}
            className="flex items-center gap-1 rounded py-1 text-muted-foreground hover:text-foreground">
            <ChevronRight className={cn("size-3.5 transition-transform", notFitOpen && "rotate-90")} aria-hidden />
            Not a fit <span className="tabular-nums">{notFit.length}</span>
          </button>
          {notFitOpen && <div className={cn(GRID, "mt-2")}>{notFit.map(card)}</div>}
        </section>
      )}
    </div>
  )
}
