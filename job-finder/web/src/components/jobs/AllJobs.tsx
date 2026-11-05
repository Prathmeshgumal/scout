import {
  DndContext, KeyboardSensor, MouseSensor, TouchSensor, closestCenter, useSensor, useSensors, type CollisionDetection, type DragEndEvent,
} from "@dnd-kit/core"
import { SortableContext, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy } from "@dnd-kit/sortable"
import { CSS } from "@dnd-kit/utilities"
import { ChevronLeft, Search } from "lucide-react"
import { useEffect, useMemo, useState } from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select } from "@/components/ui/select"
import { useBoardOrder } from "@/components/board/useBoardOrder"
import { RolePage } from "@/components/pipeline/RolePage"
import { useJobDetail, usePipeline } from "@/components/pipeline/usePipeline"
import { listJobs, type JobCardData, type PipelineStage, type Verdict } from "@/lib/api"
import { NO_FILTERS, VERDICT_LABEL, filterJobs, groupByGoal, isClosed, stageLabel, workplaces } from "@/lib/jobs"
import { inOrder, moveCard, placeVisible } from "@/lib/board-order"
import { cn } from "@/lib/utils"
import { SkeletonRows, VerdictBadge } from "./JobRow"
import { ROLE_CARD, ROLE_CARD_ACTIVE, RoleCardBody } from "./RoleCardBody"

const NOT_WORKING = { research: false, check: false }
const COLUMNS = "grid grid-cols-[repeat(5,minmax(216px,1fr))] gap-2"

type ColumnId = Verdict | "progress"
const BOARD: { id: ColumnId; label: string; dot: string }[] = [
  { id: "fit", label: VERDICT_LABEL.fit, dot: "bg-emerald-500" },
  { id: "partial", label: VERDICT_LABEL.partial, dot: "bg-amber-500" },
  { id: "unsure", label: VERDICT_LABEL.unsure, dot: "bg-violet-500" },
  { id: "progress", label: "In progress", dot: "border border-dashed border-muted-foreground" },
  { id: "not_fit", label: VERDICT_LABEL.not_fit, dot: "bg-muted-foreground/50" },
]

const SCREEN_READER_HELP = { draggable: "To open this role, press Enter. To change its priority, press Space, move it up or down with the arrow keys, and press Space again." }

/** A card of the board. Drag it up or down to change its place in its column. */
function JobCard({ job, onOpen }: { job: JobCardData; onOpen: () => void }) {
  const { attributes, listeners, setNodeRef, isDragging, transform, transition } = useSortable({ id: job.id, attributes: { roleDescription: "draggable role" } })
  return (
    <button ref={setNodeRef} type="button" {...attributes} {...listeners} onClick={onOpen} style={{ transform: CSS.Translate.toString(transform), transition }}
      className={cn(ROLE_CARD, ROLE_CARD_ACTIVE, "touch-manipulation", isDragging && "relative z-10 shadow-lg ring-1 ring-violet-500/45")}>
      <RoleCardBody company={job.company_name} title={job.title} location={job.location} posted={job.posted_at} closed={isClosed(job)}
        badge={job.fit ? undefined : <VerdictBadge verdict={null} stage={stageLabel(job, NOT_WORKING)} />} />
    </button>
  )
}

type JobPageProps = {
  job: JobCardData
  draft: string
  onDraft: (text: string) => void
  onBack: () => void
  onOpenGoal: (id: string) => void
  onShowInPipeline: (roleKey: string) => void
}

/** The page of one role from the Jobs board. The role can be in the pipeline or not. */
function JobPage({ job, draft, onDraft, onBack, onOpenGoal, onShowInPipeline }: JobPageProps) {
  const { detail, failed } = useJobDetail(job.goal_id, job.id)
  const { items, removed, read, move } = usePipeline()
  // A role that was not in the pipeline has no row to change on the screen. This shows the new stage until the server answers.
  const [pending, setPending] = useState<PipelineStage | "removed" | null>(null)
  const roleKey = detail?.pipeline.role_key ?? null
  const tracked = items?.find((item) => item.role_key === roleKey)
  const saved = tracked?.stage ?? (items ? null : detail?.pipeline.stage ?? null)
  const stage = pending === "removed" ? null : pending ?? saved
  return (
    <RolePage role={{ ...job, job_id: job.id }} roleKey={roleKey} stage={stage} stageKnown={Boolean(detail)} notes={(tracked ?? removed?.find((item) => item.role_key === roleKey))?.notes ?? []} detail={detail} failed={failed}
      backLabel="Jobs" noVerdict={stageLabel(job, NOT_WORKING)} draft={draft} onDraft={onDraft} onBack={onBack} onChanged={() => void read()} onOpenGoal={onOpenGoal}
      onShowInPipeline={roleKey ? () => onShowInPipeline(roleKey) : undefined}
      onMove={(next) => {
        if (!roleKey) return
        setPending(next)
        void move({ role_key: roleKey, title: job.title, stage }, next).finally(() => setPending(null))
      }} />
  )
}

type Props = {
  /** The role whose page is open, or null for the board. */
  openJob: { goalId: string; jobId: string } | null
  onOpenJob: (job: JobCardData) => void
  onBoard: () => void
  onShowInPipeline: (roleKey: string) => void
  onNewGoal: () => void
  onOpenGoal: (id: string) => void
}

/** All roles as a board, with one column for each verdict. The agents give the verdict, so a card stays in its column; the user drags it up or down to set its priority. */
export function AllJobs({ openJob, onOpenJob, onBoard, onShowInPipeline, onNewGoal, onOpenGoal }: Props) {
  const [jobs, setJobs] = useState<JobCardData[] | null>(null)
  const [failed, setFailed] = useState(false)
  const [goal, setGoal] = useState("all")
  const [workplace, setWorkplace] = useState("all")
  const [text, setText] = useState("")
  // A note that is not saved stays when the user goes back to the board.
  const [drafts, setDrafts] = useState<Record<string, string>>({})

  useEffect(() => {
    let active = true
    const read = () => listJobs().then((list) => { if (active) { setJobs(list); setFailed(false) } }).catch(() => { if (active) setFailed(true) })
    void read()
    const timer = setInterval(() => void read(), 10_000)
    return () => { active = false; clearInterval(timer) }
  }, [])

  const goals = useMemo(() => groupByGoal(jobs ?? []), [jobs])
  const places = useMemo(() => workplaces(jobs ?? []), [jobs])
  const shown = useMemo(() => (jobs ?? []).filter((job) => goal === "all" || job.goal_id === goal), [jobs, goal])
  const order = useBoardOrder("jobs")
  const inColumn = (id: ColumnId) => inOrder(filterJobs(shown, { ...NO_FILTERS, verdict: id, workplace, text }), (job) => job.id, id, order.positions)
  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 5 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 200, tolerance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates, keyboardCodes: { start: ["Space"], cancel: ["Escape"], end: ["Space", "Enter"] } }),
  )
  const columnOf = (id: string) => BOARD.find((column) => inColumn(column.id).some((job) => job.id === id))?.id ?? null
  // A card stays in its column: only the cards of that column count.
  const sameColumn: CollisionDetection = (args) => {
    const column = columnOf(String(args.active.id))
    return closestCenter({ ...args, droppableContainers: args.droppableContainers.filter((c) => columnOf(String(c.id)) === column) })
  }
  const drop = ({ active, over }: DragEndEvent) => {
    const column = columnOf(String(active.id))
    if (!over || !column || over.id === active.id) return
    const visible = inColumn(column).map((job) => job.id)
    const moved = moveCard({ [column]: visible }, String(active.id), column, visible.indexOf(String(over.id)))[column]
    const full = inOrder(filterJobs(jobs ?? [], { ...NO_FILTERS, verdict: column }), (job) => job.id, column, order.positions).map((job) => job.id)
    void order.save(column, placeVisible(full, moved))
  }

  if (openJob) {
    const job = jobs?.find((row) => row.id === openJob.jobId && row.goal_id === openJob.goalId)
    if (job) {
      return <JobPage key={job.id} job={job} draft={drafts[job.id] ?? ""} onDraft={(next) => setDrafts((now) => ({ ...now, [job.id]: next }))} onBack={onBoard} onOpenGoal={onOpenGoal} onShowInPipeline={onShowInPipeline} />
    }
    return (
      <div className="flex flex-col items-start gap-3 p-5">
        {!jobs && !failed && <SkeletonRows count={3} />}
        {failed && !jobs && <p className="text-red-700 dark:text-red-300">Could not read the roles. The page tries again in 10 seconds.</p>}
        {jobs && <p className="text-muted-foreground">This role does not exist. Its mission can be deleted.</p>}
        <Button size="sm" variant="outline" onClick={onBoard}><ChevronLeft aria-hidden /> Jobs</Button>
      </div>
    )
  }

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      <header className="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-2 px-6 pt-4">
        <h1 className="mr-auto text-[17px] font-semibold">Jobs {jobs && jobs.length > 0 && <span className="font-normal text-muted-foreground tabular-nums">{jobs.length}</span>}</h1>
        {jobs && jobs.length > 0 && (
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            {goals.length > 1 && (
              <Select label="Filter by mission" value={goal} onChange={setGoal} className="h-8 max-w-[260px] rounded-md bg-card text-[13px]"
                choices={[{ value: "all", label: "All missions" }, ...goals.map((g) => ({ value: g.goalId, label: g.title }))]} />
            )}
            {places.length > 1 && (
              <Select label="Filter by workplace" value={workplace} onChange={setWorkplace} className="h-8 rounded-md bg-card text-[13px]"
                choices={[{ value: "all", label: "Any workplace" }, ...places.map((p) => ({ value: p, label: p[0].toUpperCase() + p.slice(1) }))]} />
            )}
            <div className="relative w-[220px] max-w-full">
              <Search className="absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-muted-foreground" aria-hidden />
              <Input aria-label="Search roles" placeholder="Search roles" value={text} onChange={(e) => setText(e.target.value)} className="h-8 pl-7 text-[13px] md:text-[13px]" />
            </div>
          </div>
        )}
      </header>
      {failed && !jobs && <p className="px-6 py-3 text-red-700 dark:text-red-300">Could not read the roles. The page tries again in 10 seconds.</p>}
      {!jobs && !failed && <div className="p-4"><SkeletonRows /></div>}
      {jobs?.length === 0 && (
        <div className="flex flex-col items-start gap-2 px-6 py-6">
          <p className="font-medium">No roles yet.</p>
          <p className="text-muted-foreground">Start a mission and say what you want. The roles of each mission appear here.</p>
          <Button size="sm" onClick={onNewGoal}>Start a mission</Button>
        </div>
      )}
      {jobs && jobs.length > 0 && (
        <div className="min-h-0 flex-1 overflow-auto px-4 pb-4">
          <div className="flex min-h-full min-w-[1112px] flex-col">
            <div className={cn(COLUMNS, "sticky top-0 z-10 bg-background py-3")}>
              {BOARD.map((column) => (
                <h2 key={column.id} className="flex items-center gap-2 px-2 font-medium">
                  <span className={cn("size-2 shrink-0 rounded-full", column.dot)} aria-hidden />
                  <span className="truncate">{column.label}</span>
                  <span className="text-muted-foreground tabular-nums">{inColumn(column.id).length}</span>
                </h2>
              ))}
            </div>
            <DndContext sensors={sensors} collisionDetection={sameColumn} onDragEnd={drop} accessibility={{ screenReaderInstructions: SCREEN_READER_HELP }}>
            <div className={cn(COLUMNS, "flex-1")}>
              {BOARD.map((column) => {
                const rows = inColumn(column.id)
                return (
                  <section key={column.id} aria-label={column.label} className="rounded-xl bg-muted/35 p-1.5">
                    {rows.length === 0 ? <p className="px-2 py-3 text-[12.5px] text-muted-foreground">No roles</p> : (
                      <SortableContext items={rows.map((job) => job.id)} strategy={verticalListSortingStrategy}>
                        <ul className="flex flex-col gap-1.5">
                          {rows.map((job) => <li key={job.id}><JobCard job={job} onOpen={() => onOpenJob(job)} /></li>)}
                        </ul>
                      </SortableContext>
                    )}
                  </section>
                )
              })}
            </div>
            </DndContext>
          </div>
        </div>
      )}
    </div>
  )
}
