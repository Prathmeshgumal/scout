import { ChevronRight, CircleAlert, CircleStop, Clock, Loader2, Pause, ScrollText, Search, SquarePen, Target, Trash2 } from "lucide-react"
import { useState, type ReactNode } from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import type { GoalStatus, GoalSummary, Mandate } from "@/lib/api"
import { cn } from "@/lib/utils"

const ICON = "size-4 shrink-0 text-muted-foreground"

/** The state of a goal, at the right of its row. A goal that is done or that the user stopped needs no icon. */
function StatusIcon({ status }: { status: GoalStatus }) {
  if (status === "running") return <Loader2 role="img" aria-label="Working" className="size-3.5 shrink-0 animate-spin text-violet-600 motion-reduce:animate-none dark:text-violet-400" />
  if (status === "waiting") return <Clock role="img" aria-label="Waiting on scrapes" className="size-3.5 shrink-0 text-amber-500" />
  if (status === "failed") return <CircleAlert role="img" aria-label="Failed" className="size-3.5 shrink-0 text-red-600 dark:text-red-400" />
  if (status === "capped") return <CircleAlert role="img" aria-label="Reached its limit" className="size-3.5 shrink-0 text-amber-500" />
  return null
}

/** A scroll, with a green dot with ripples while a goal of the mandate works. A paused or ended mandate shows its state. */
function MandateIcon({ status, working }: { status: Mandate["status"]; working: boolean }) {
  if (status === "paused") return <Pause role="img" aria-label="Paused" className={ICON} />
  if (status === "ended") return <CircleStop role="img" aria-label="Ended" className={ICON} />
  return (
    <span className="relative shrink-0">
      <ScrollText aria-hidden className={ICON} />
      {working && (
        <span role="img" aria-label="Working now" className="absolute -top-0.5 -right-0.5 size-2 rounded-full bg-emerald-500 ring-2 ring-background">
          <span className="absolute inset-0 animate-ping rounded-full bg-emerald-500 motion-reduce:animate-none" />
        </span>
      )}
    </span>
  )
}

function Row({ title, meta, icon, status, active, indent, onOpen, onDelete, before }: {
  title: string; meta?: string; icon?: ReactNode; status?: ReactNode; active: boolean; indent?: boolean
  onOpen: () => void; onDelete?: () => void; before?: ReactNode
}) {
  return (
    <div className={cn("group flex items-center rounded-lg", active ? "bg-violet-500/10" : "hover:bg-muted/70", indent && "ml-3")}>
      {before}
      <button type="button" onClick={onOpen} aria-current={active ? "true" : undefined}
        className="flex min-w-0 flex-1 items-center gap-2.5 rounded-lg px-2 py-1.5 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring">
        {icon}
        <span className="min-w-0 flex-1">
          <span className="block truncate">{title}</span>
          {meta && <span className="block truncate text-[11.5px] text-muted-foreground tabular-nums">{meta}</span>}
        </span>
        {status}
      </button>
      {onDelete && (
        <button type="button" aria-label={`Delete ${title}`} onClick={onDelete}
          className="mr-1 grid size-6 shrink-0 place-items-center rounded text-muted-foreground opacity-0 outline-none group-hover:opacity-100 hover:text-red-600 focus-visible:opacity-100 focus-visible:ring-2 focus-visible:ring-ring [&_svg]:size-3.5 [@media(hover:none)]:opacity-100">
          <Trash2 />
        </button>
      )}
    </div>
  )
}

const goalMeta = (g: GoalSummary) => (g.jobs ? `${g.jobs} role${g.jobs === 1 ? "" : "s"}, ${g.fit} fit` : g.status === "running" ? "Working" : "No roles")

function Pane({ title, count, onNew, newLabel, full, search, setSearch, children }: {
  title: string; count: number; onNew: () => void; newLabel: string; full: boolean
  search: string; setSearch: (text: string) => void; children: ReactNode
}) {
  return (
    <section aria-label={title} className={cn("flex min-h-0 shrink-0 flex-col", full ? "w-full flex-1" : "w-[260px] border-r")}>
      <header className="flex h-11 shrink-0 items-center gap-2 px-3">
        <h2 className="font-semibold">{title}</h2>
        <span className="text-[12px] text-muted-foreground tabular-nums">{count}</span>
        <span className="flex-1" />
        <Button size="icon" variant="ghost" className="size-7" aria-label={newLabel} onClick={onNew}><SquarePen /></Button>
      </header>
      <div className="relative mx-2 mb-2">
        <Search className="absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-muted-foreground" aria-hidden />
        <Input aria-label={`Search ${title.toLowerCase()}`} placeholder="Search" value={search} onChange={(e) => setSearch(e.target.value)} className="h-7 pl-7 text-[12px] md:text-[12px]" />
      </div>
      <div className="flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto px-2 pb-2">{children}</div>
    </section>
  )
}

type GoalProps = { goals: GoalSummary[]; activeId: string | null; full: boolean; onOpen: (id: string) => void; onNew: () => void; onDelete: (goal: GoalSummary) => void }

export function GoalList({ goals, activeId, full, onOpen, onNew, onDelete }: GoalProps) {
  const [search, setSearch] = useState("")
  const words = search.toLowerCase()
  const shown = goals.filter((g) => !g.mandate_id && !g.home_of && g.title.toLowerCase().includes(words))
  return (
    <Pane title="Goals" count={shown.length} onNew={onNew} newLabel="New goal" full={full} search={search} setSearch={setSearch}>
      {shown.length === 0 && <p className="px-2 py-3 text-muted-foreground">{search ? "No goals match." : "No goals yet. Start one to find roles."}</p>}
      {shown.map((g) => <Row key={g.id} title={g.title} meta={goalMeta(g)} icon={<Target aria-hidden className={ICON} />} status={<StatusIcon status={g.status} />} active={g.id === activeId} onOpen={() => onOpen(g.id)} onDelete={() => onDelete(g)} />)}
    </Pane>
  )
}

const SHOWN_GOALS = 5 // the newest goals of a mandate. A button shows the others.
const BUSY: GoalStatus[] = ["running", "waiting"]
const when = (iso: string) => new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })

/** What a mandate does now. A paused or ended mandate keeps its status, also while its last goal works. */
function mandateLine(m: Mandate, working: boolean) {
  if (m.status === "paused") return "Paused"
  if (m.status === "ended") return "Ended"
  if (working) return "Working now"
  return m.next_run_at ? `Next goal ${when(m.next_run_at)}` : ""
}

type MandateProps = {
  mandates: Mandate[]; goals: GoalSummary[]; activeGoalId: string | null; full: boolean
  onOpen: (id: string) => void; onOpenGoal: (id: string) => void; onNew: () => void
  onDelete: (mandate: Mandate) => void; onDeleteGoal: (goal: GoalSummary) => void
}

/** Each mandate opens its chat. Under it are the goals that it started: each one opens its chat, as any other goal. */
export function MandateList({ mandates, goals, activeGoalId, full, onOpen, onOpenGoal, onNew, onDelete, onDeleteGoal }: MandateProps) {
  const [search, setSearch] = useState("")
  const [unfolded, setUnfolded] = useState<Map<string, boolean>>(new Map()) // the user's choice for each mandate
  const [all, setAll] = useState<Set<string>>(new Set())
  const shown = mandates.filter((m) => m.title.toLowerCase().includes(search.toLowerCase()))
  const showAll = (id: string) => setAll((now) => { const next = new Set(now); if (!next.delete(id)) next.add(id); return next })
  return (
    <Pane title="Mandates" count={shown.length} onNew={onNew} newLabel="New mandate" full={full} search={search} setSearch={setSearch}>
      {shown.length === 0 && <p className="px-2 py-3 text-muted-foreground">{search ? "No mandates match." : "No mandates yet. Ask for work that repeats, for example 10 roles each weekday."}</p>}
      {shown.map((m) => {
        const runs = goals.filter((g) => g.mandate_id === m.id)
          .sort((a, b) => a.created_at.localeCompare(b.created_at))
          .map((g, i) => ({ goal: g, number: i + 1 }))
          .reverse()
        const holdsActive = m.home_goal_id === activeGoalId || runs.some(({ goal }) => goal.id === activeGoalId)
        const expanded = unfolded.get(m.id) ?? holdsActive
        const working = runs.some(({ goal }) => BUSY.includes(goal.status))
        const visible = all.has(m.id) ? runs : runs.slice(0, SHOWN_GOALS)
        return (
          <div key={m.id} className="flex flex-col gap-0.5">
            <Row title={m.title} meta={mandateLine(m, working)} icon={<MandateIcon status={m.status} working={working} />}
              active={m.home_goal_id === activeGoalId} onOpen={() => onOpen(m.id)} onDelete={() => onDelete(m)}
              before={runs.length > 0 ? (
                <button type="button" aria-label={`${expanded ? "Hide" : "Show"} the goals of ${m.title}`} aria-expanded={expanded} onClick={() => setUnfolded(new Map(unfolded).set(m.id, !expanded))}
                  className="ml-0.5 grid size-5 shrink-0 place-items-center rounded text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring">
                  <ChevronRight className={cn("size-3.5 transition-transform", expanded && "rotate-90")} aria-hidden />
                </button>
              ) : undefined} />
            {expanded && visible.map(({ goal, number }) => (
              <Row key={goal.id} indent title={goal.title || `Goal ${number}`} meta={`${when(goal.created_at)}, ${goalMeta(goal)}`} icon={<Target aria-hidden className={ICON} />} status={<StatusIcon status={goal.status} />}
                active={goal.id === activeGoalId} onOpen={() => onOpenGoal(goal.id)} onDelete={() => onDeleteGoal(goal)} />
            ))}
            {expanded && runs.length > SHOWN_GOALS && (
              <button type="button" onClick={() => showAll(m.id)} className="ml-3 self-start rounded px-2 py-1 text-[12px] text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring">
                {all.has(m.id) ? "Show fewer" : `Show ${runs.length - SHOWN_GOALS} older`}
              </button>
            )}
          </div>
        )
      })}
    </Pane>
  )
}
