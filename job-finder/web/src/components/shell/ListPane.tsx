import { ChevronRight, Plus, Search, Trash2 } from "lucide-react"
import { useState, type ReactNode } from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import type { GoalStatus, GoalSummary, Mandate } from "@/lib/api"
import { cn } from "@/lib/utils"

const DOT: Record<GoalStatus, string> = {
  running: "bg-violet-500 shadow-[0_0_8px] shadow-violet-500",
  waiting: "bg-amber-500",
  done: "bg-emerald-500",
  capped: "bg-amber-500",
  failed: "bg-red-500",
  stopped: "bg-muted-foreground/50",
}
const MANDATE_DOT = { active: "bg-emerald-500", paused: "bg-amber-500", ended: "bg-muted-foreground/50" }

function Row({ title, meta, dot, label, active, indent, onOpen, onDelete, before }: {
  title: string; meta?: string; dot: string; label: string; active: boolean; indent?: boolean
  onOpen: () => void; onDelete?: () => void; before?: ReactNode
}) {
  return (
    <div className={cn("group flex items-center rounded-lg", active ? "bg-violet-500/10" : "hover:bg-muted/70", indent && "ml-3")}>
      {before}
      <button type="button" onClick={onOpen} aria-current={active ? "true" : undefined}
        className="flex min-w-0 flex-1 items-center gap-2.5 rounded-lg px-2 py-1.5 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring">
        <i className={cn("size-1.5 shrink-0 rounded-full", dot)} role="img" aria-label={label} />
        <span className="min-w-0 flex-1">
          <span className="block truncate">{title}</span>
          {meta && <span className="block truncate text-[11.5px] text-muted-foreground tabular-nums">{meta}</span>}
        </span>
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
        <Button size="icon" variant="ghost" className="size-7" aria-label={newLabel} onClick={onNew}><Plus /></Button>
      </header>
      <div className="relative mx-2 mb-2">
        <Search className="absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-muted-foreground" aria-hidden />
        <Input aria-label={`Search ${title.toLowerCase()}`} placeholder="Search" value={search} onChange={(e) => setSearch(e.target.value)} className="h-7 pl-7 text-[12px] md:text-[12px]" />
      </div>
      <div className="flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto px-2 pb-2">{children}</div>
    </section>
  )
}

type ChatProps = { goals: GoalSummary[]; activeId: string | null; full: boolean; onOpen: (id: string) => void; onNew: () => void; onDelete: (goal: GoalSummary) => void }

export function ChatList({ goals, activeId, full, onOpen, onNew, onDelete }: ChatProps) {
  const [search, setSearch] = useState("")
  const words = search.toLowerCase()
  const chats = goals.filter((g) => !g.mandate_id && !g.home_of && g.title.toLowerCase().includes(words))
  return (
    <Pane title="Chats" count={chats.length} onNew={onNew} newLabel="New chat" full={full} search={search} setSearch={setSearch}>
      {chats.length === 0 && <p className="px-2 py-3 text-muted-foreground">{search ? "No chats match." : "No chats yet. Start one to find roles."}</p>}
      {chats.map((g) => <Row key={g.id} title={g.title} meta={goalMeta(g)} dot={DOT[g.status]} label={g.status} active={g.id === activeId} onOpen={() => onOpen(g.id)} onDelete={() => onDelete(g)} />)}
    </Pane>
  )
}

type MandateProps = {
  mandates: Mandate[]; goals: GoalSummary[]; activeId: string | null; activeGoalId: string | null; full: boolean
  onOpen: (id: string) => void; onOpenGoal: (id: string) => void; onNew: () => void; onDelete: (mandate: Mandate) => void
}

export function MandateList({ mandates, goals, activeId, activeGoalId, full, onOpen, onOpenGoal, onNew, onDelete }: MandateProps) {
  const [search, setSearch] = useState("")
  const [open, setOpen] = useState<Set<string>>(new Set())
  const shown = mandates.filter((m) => m.title.toLowerCase().includes(search.toLowerCase()))
  const toggle = (id: string) => setOpen((now) => { const next = new Set(now); if (!next.delete(id)) next.add(id); return next })
  return (
    <Pane title="Mandates" count={shown.length} onNew={onNew} newLabel="New mandate" full={full} search={search} setSearch={setSearch}>
      {shown.length === 0 && <p className="px-2 py-3 text-muted-foreground">{search ? "No mandates match." : "No mandates yet. Ask for work that repeats in a chat, for example 10 roles each weekday."}</p>}
      {shown.map((m) => {
        const runs = goals.filter((g) => g.mandate_id === m.id)
        const expanded = open.has(m.id)
        return (
          <div key={m.id} className="flex flex-col gap-0.5">
            <Row title={m.title} meta={m.status} dot={MANDATE_DOT[m.status]} label={m.status} active={m.id === activeId} onOpen={() => onOpen(m.id)} onDelete={() => onDelete(m)}
              before={(
                <button type="button" aria-label={`${expanded ? "Hide" : "Show"} the goals of ${m.title}`} aria-expanded={expanded} onClick={() => toggle(m.id)}
                  className="ml-0.5 grid size-5 shrink-0 place-items-center rounded text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring">
                  <ChevronRight className={cn("size-3.5 transition-transform", expanded && "rotate-90")} aria-hidden />
                </button>
              )} />
            {expanded && runs.length === 0 && <p className="ml-8 py-1 text-[12px] text-muted-foreground">No goals yet.</p>}
            {expanded && runs.map((g) => (
              <Row key={g.id} indent title={g.run_number ? `Run ${g.run_number}` : g.title} meta={goalMeta(g)} dot={DOT[g.status]} label={g.status} active={g.id === activeGoalId} onOpen={() => onOpenGoal(g.id)} />
            ))}
          </div>
        )
      })}
    </Pane>
  )
}
