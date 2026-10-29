import { ExternalLink } from "lucide-react"
import type { JobCardData, Verdict } from "@/lib/api"
import { EVIDENCE_LABEL, VERDICT_LABEL, isClosed, monogram, monogramHue, stageLabel } from "@/lib/jobs"
import { cn } from "@/lib/utils"

const BADGE: Record<Verdict, string> = {
  fit: "bg-emerald-500/15 text-emerald-700 ring-emerald-500/30 dark:text-emerald-300",
  partial: "bg-amber-500/15 text-amber-800 ring-amber-500/30 dark:text-amber-300",
  unsure: "bg-transparent text-violet-700 ring-violet-500/50 dark:text-violet-300",
  not_fit: "bg-muted text-muted-foreground ring-border",
}

export function VerdictBadge({ verdict, stage }: { verdict: Verdict | null; stage?: string }) {
  return (
    <span className={cn("inline-flex h-[22px] shrink-0 items-center rounded-md px-2 text-[11.5px] font-medium ring-1 ring-inset",
      verdict ? BADGE[verdict] : "text-muted-foreground ring-border")}>
      {verdict ? VERDICT_LABEL[verdict] : stage}
    </span>
  )
}

export function Monogram({ company }: { company: string }) {
  const hue = monogramHue(company)
  return (
    <span
      aria-hidden
      style={{ backgroundColor: `oklch(0.62 0.13 ${hue} / 0.16)`, color: `light-dark(oklch(0.42 0.13 ${hue}), oklch(0.82 0.11 ${hue}))` }}
      className="grid size-9 shrink-0 place-items-center rounded-lg text-[12px] font-semibold tracking-wide [color-scheme:light] dark:[color-scheme:dark]"
    >
      {monogram(company)}
    </span>
  )
}

type Props = {
  job: JobCardData
  working: { research: boolean; check: boolean }
  selected?: boolean
  onOpen: (job: JobCardData) => void
  /** The title of the goal: on the page of all roles. */
  goalTitle?: string
}

export function JobCard({ job, working, selected, onOpen, goalTitle }: Props) {
  const facts = [job.location, job.workplace_type, job.posted_at && `Posted ${job.posted_at}`].filter(Boolean)
  const closed = isClosed(job)
  return (
    <article className={cn("role-card card-in relative flex min-w-0 flex-col gap-2.5 rounded-xl border bg-card p-3.5", selected && "border-violet-500/50")}>
      <div className="flex min-w-0 items-start gap-3">
        <Monogram company={job.company_name} />
        <div className="min-w-0 flex-1">
          <h3 className="line-clamp-2 text-[14px] leading-[1.3] font-semibold break-words">
            <button type="button" onClick={() => onOpen(job)} className="text-left outline-none after:absolute after:inset-0 after:rounded-xl focus-visible:after:ring-2 focus-visible:after:ring-ring">
              {job.title}
            </button>
          </h3>
          <p className="mt-0.5 truncate text-muted-foreground">{job.company_name}</p>
        </div>
        <VerdictBadge verdict={job.fit} stage={stageLabel(job, working)} />
      </div>
      {facts.length > 0 && <p className="truncate text-[12px] text-muted-foreground">{facts.join(" · ")}</p>}
      {job.fit_reasons && <p className="line-clamp-2 leading-[1.45] break-words">{job.fit_reasons}</p>}
      {goalTitle && <p className="truncate text-[12px] text-muted-foreground">From {goalTitle}</p>}
      <div className="mt-auto flex items-center justify-between gap-3 pt-0.5 text-[12px] text-muted-foreground">
        <span className="min-w-0 truncate">{EVIDENCE_LABEL[job.evidence_level]}</span>
        {closed ? <span className="shrink-0 font-medium">Closed</span> : (
          <a href={job.url} target="_blank" rel="noopener noreferrer"
            className="relative z-10 flex shrink-0 items-center gap-1 rounded text-violet-700 hover:underline dark:text-violet-300">
            Open role <ExternalLink className="size-3" aria-hidden />
          </a>
        )}
      </div>
    </article>
  )
}

export function SkeletonCards({ count = 6 }: { count?: number }) {
  return (
    <div className="grid grid-cols-[repeat(auto-fill,minmax(min(100%,300px),1fr))] gap-3" role="status" aria-label="Loading roles">
      {Array.from({ length: count }, (_, i) => (
        <div key={i} className="flex flex-col gap-3 rounded-xl border bg-card p-3.5">
          <div className="flex gap-3">
            <div className="skeleton size-9 shrink-0" />
            <div className="flex flex-1 flex-col gap-2 pt-0.5"><div className="skeleton h-3.5 w-4/5" /><div className="skeleton h-3 w-1/2" /></div>
          </div>
          <div className="skeleton h-3 w-2/3" />
          <div className="skeleton h-3 w-full" />
        </div>
      ))}
    </div>
  )
}
