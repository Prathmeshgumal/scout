import { ExternalLink } from "lucide-react"
import type { JobCardData, Verdict } from "@/lib/api"
import { VERDICT_LABEL, isClosed, monogram, monogramHue, stageLabel } from "@/lib/jobs"
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

export function JobRow({ job, working, selected, onOpen, goalTitle }: Props) {
  const facts = [job.company_name, job.location, job.workplace_type, job.posted_at && `Posted ${job.posted_at}`].filter(Boolean)
  return (
    <li className={cn("relative grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 px-3 py-2.5 transition-colors hover:bg-muted/50 focus-within:bg-muted/50 sm:grid-cols-[auto_minmax(0,1fr)_auto]", selected && "bg-violet-500/8")}>
      <Monogram company={job.company_name} />
      <div className="min-w-0">
        <h3 className="truncate text-[14px] leading-[1.35] font-medium">
          <button type="button" onClick={() => onOpen(job)} className="text-left outline-none after:absolute after:inset-0 focus-visible:after:ring-2 focus-visible:after:ring-violet-500/50 focus-visible:after:ring-inset">
            {job.title}
          </button>
        </h3>
        <p className="truncate text-[12.5px] text-muted-foreground">{facts.join(" · ")}</p>
        {job.fit_reasons && <p className="mt-0.5 line-clamp-1 text-[13px] break-words">{job.fit_reasons}</p>}
        {goalTitle && <p className="mt-0.5 truncate text-[12px] text-muted-foreground">From {goalTitle}</p>}
      </div>
      <div className="col-start-2 mt-1.5 flex items-center gap-3 sm:col-start-3 sm:mt-0 sm:self-start">
        <VerdictBadge verdict={job.fit} stage={stageLabel(job, working)} />
        {isClosed(job) ? <span className="w-[74px] text-right text-[12px] font-medium text-muted-foreground">Closed</span> : (
          <a href={job.url} target="_blank" rel="noopener noreferrer"
            className="relative z-10 flex w-[74px] items-center justify-end gap-1 rounded text-[12px] text-violet-700 hover:underline dark:text-violet-300">
            Open role <ExternalLink className="size-3" aria-hidden />
          </a>
        )}
      </div>
    </li>
  )
}

export function SkeletonRows({ count = 6 }: { count?: number }) {
  return (
    <div className="divide-y rounded-xl border" role="status" aria-label="Loading roles">
      {Array.from({ length: count }, (_, i) => (
        <div key={i} className="flex gap-3 px-3 py-2.5">
          <div className="skeleton size-9 shrink-0" />
          <div className="flex flex-1 flex-col gap-2 pt-0.5"><div className="skeleton h-3.5 w-1/2" /><div className="skeleton h-3 w-3/4" /></div>
        </div>
      ))}
    </div>
  )
}
