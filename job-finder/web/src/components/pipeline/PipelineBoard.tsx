import {
  DndContext, DragOverlay, KeyboardSensor, MouseSensor, TouchSensor, useDraggable, useDroppable, useSensor, useSensors,
  type Announcements, type CollisionDetection, type DragEndEvent, type DragStartEvent, type KeyboardCoordinateGetter,
} from "@dnd-kit/core"
import { Archive } from "lucide-react"
import { useEffect, useMemo, useRef, useState } from "react"
import { VerdictBadge } from "@/components/jobs/JobRow"
import { ROLE_CARD, ROLE_CARD_ACTIVE, RoleCardBody } from "@/components/jobs/RoleCardBody"
import type { PipelineItem, PipelineStage, RemovedItem } from "@/lib/api"
import { isClosed } from "@/lib/jobs"
import { STAGES, funnelOf } from "@/lib/pipeline"
import { cn } from "@/lib/utils"
import { Funnel } from "./Funnel"

const COLUMNS = "grid grid-cols-[repeat(4,minmax(248px,1fr))] gap-2"
const COLUMNS_WITH_REMOVED = "grid grid-cols-[repeat(4,minmax(248px,1fr))_minmax(220px,0.8fr)] gap-2"
const REMOVED = { id: "removed", label: "Removed" } as const
type Card = PipelineItem | RemovedItem
type ColumnId = PipelineStage | "removed"
const stageIds: string[] = [...STAGES.map((stage) => stage.id), REMOVED.id]
const labelOf = (id: unknown) => [...STAGES, REMOVED].find((stage) => stage.id === id)?.label ?? ""

/** The column under the pointer, or under the card when the keyboard moves it. Only left and right count, so a drop works anywhere in a column. */
const byColumn: CollisionDetection = ({ droppableContainers, droppableRects, pointerCoordinates, collisionRect }) => {
  const x = pointerCoordinates?.x ?? collisionRect.left + collisionRect.width / 2
  const nearest = droppableContainers
    .flatMap((container) => {
      const rect = droppableRects.get(container.id)
      return rect ? [{ container, distance: x < rect.left ? rect.left - x : x > rect.right ? x - rect.right : 0 }] : []
    })
    .sort((a, b) => a.distance - b.distance)[0]
  return nearest ? [{ id: nearest.container.id, data: { droppableContainer: nearest.container, value: nearest.distance } }] : []
}

/** Left and right arrows jump one column. */
const jumpColumn: KeyboardCoordinateGetter = (event, { active, currentCoordinates, context }) => {
  const step = event.code === "ArrowRight" ? 1 : event.code === "ArrowLeft" ? -1 : 0
  if (!step || !context.collisionRect) return undefined
  event.preventDefault()
  const current = String(context.over?.id ?? context.droppableContainers.get(active)?.id ?? "")
  const target = context.droppableRects.get(stageIds[stageIds.indexOf(current) + step])
  if (!target) return undefined
  const middle = context.collisionRect.left + context.collisionRect.width / 2
  return { x: currentCoordinates.x + target.left + target.width / 2 - middle, y: currentCoordinates.y }
}

const SCREEN_READER_HELP = { draggable: "To open this role, press Enter. To move it, press Space, choose a column with the left and right arrows, and press Space again." }

function CardBody({ item }: { item: Card }) {
  return <RoleCardBody company={item.company_name} title={item.title} location={item.location} noteCount={item.notes.length} closed={isClosed(item)} badge={item.fit && <VerdictBadge verdict={item.fit} />} />
}

function RoleCard({ item, onOpen, lit }: { item: Card; onOpen: () => void; lit: boolean }) {
  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({ id: item.role_key, data: { item }, attributes: { roleDescription: "draggable role" } })
  const node = useRef<HTMLButtonElement | null>(null)
  useEffect(() => {
    if (!lit || !node.current) return
    node.current.focus({ preventScroll: true })
    node.current.scrollIntoView({ block: "center", inline: "center" })
  }, [lit])
  return (
    <button ref={(el) => { node.current = el; setNodeRef(el) }} type="button" {...attributes} {...listeners} onClick={onOpen} data-highlighted={lit || undefined}
      className={cn(ROLE_CARD, ROLE_CARD_ACTIVE, "touch-manipulation transition-[background-color,border-color,box-shadow] duration-500", isDragging && "opacity-35",
        item.stage === "removed" && "bg-transparent",
        lit && "border-violet-500 bg-violet-500/15 ring-2 ring-violet-500/60")}>
      <CardBody item={item} />
    </button>
  )
}

function Column({ stage, rows, onOpen, lit }: { stage: { id: ColumnId; label: string }; rows: Card[]; onOpen: (roleKey: string) => void; lit: string | null }) {
  const { setNodeRef, isOver } = useDroppable({ id: stage.id })
  return (
    <section ref={setNodeRef} aria-label={stage.label}
      className={cn("rounded-xl bg-muted/35 p-1.5 ring-1 ring-transparent transition-colors ring-inset", isOver && "bg-violet-500/10 ring-violet-500/45")}>
      {rows.length === 0 ? <p className="px-2 py-3 text-[12.5px] text-muted-foreground">No roles</p> : (
        <ul className="flex flex-col gap-1.5">
          {rows.map((item) => <li key={item.role_key}><RoleCard item={item} lit={lit === item.role_key} onOpen={() => onOpen(item.role_key)} /></li>)}
        </ul>
      )}
    </section>
  )
}

type Props = {
  items: PipelineItem[]
  /** The removed roles when the user shows their column. */
  removed: RemovedItem[] | null
  /** The card to show when the board opens. */
  highlight: string | null
  onMove: (item: Card, stage: ColumnId) => void
  onOpen: (roleKey: string) => void
  /** The page stops reading new data while a card is in the air. */
  onDragging: (dragging: boolean) => void
}

/** The roles as columns, one column for each stage. A role moves when its card is dropped on another column. */
export function PipelineBoard({ items, removed, highlight, onMove, onOpen, onDragging }: Props) {
  const [active, setActive] = useState<Card | null>(null)
  const [lit, setLit] = useState<string | null>(highlight)
  useEffect(() => {
    setLit(highlight)
    if (!highlight) return
    const timer = setTimeout(() => setLit(null), 2600)
    return () => clearTimeout(timer)
  }, [highlight])
  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 5 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 200, tolerance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: jumpColumn, keyboardCodes: { start: ["Space"], cancel: ["Escape"], end: ["Space", "Enter"] } }),
  )
  const titleOf = (id: unknown) => [...items, ...(removed ?? [])].find((item) => item.role_key === id)?.title ?? "The role"
  const announcements: Announcements = {
    onDragStart: ({ active: card }) => `Picked up ${titleOf(card.id)}.`,
    onDragOver: ({ active: card, over }) => (over ? `${titleOf(card.id)} is over ${labelOf(over.id)}.` : undefined),
    onDragEnd: ({ active: card, over }) => (over ? `${titleOf(card.id)} moved to ${labelOf(over.id)}.` : `${titleOf(card.id)} stayed in its stage.`),
    onDragCancel: ({ active: card }) => `${titleOf(card.id)} stayed in its stage.`,
  }

  const start = ({ active: card }: DragStartEvent) => {
    setActive((card.data.current as { item: Card }).item)
    onDragging(true)
  }
  const stop = () => {
    setActive(null)
    onDragging(false)
  }
  const end = ({ active: card, over }: DragEndEvent) => {
    stop()
    const item = (card.data.current as { item: Card }).item
    if (over && over.id !== item.stage) onMove(item, over.id as ColumnId)
  }

  const grid = removed ? COLUMNS_WITH_REMOVED : COLUMNS
  const steps = useMemo(() => funnelOf(items), [items])
  const counts = useMemo(() => Object.fromEntries(STAGES.map((stage) => [stage.id, items.filter((item) => item.stage === stage.id).length])), [items])
  return (
    <DndContext sensors={sensors} collisionDetection={byColumn} onDragStart={start} onDragEnd={end} onDragCancel={stop}
      accessibility={{ announcements, screenReaderInstructions: SCREEN_READER_HELP }}>
      <div className="min-h-0 flex-1 overflow-auto px-4 pb-4">
        <div className={cn("flex min-h-full flex-col", removed ? "min-w-[1252px]" : "min-w-[1016px]")}>
          <Funnel steps={steps} counts={counts} className={cn(grid, "sticky top-0 z-10 bg-background py-3")}>
            {removed && (
              <li className="flex min-w-0 flex-col gap-1.5 px-2">
                <h2 className="flex items-center gap-2 font-medium text-muted-foreground">
                  <Archive className="size-3.5 shrink-0" aria-hidden />
                  <span className="truncate">{REMOVED.label}</span>
                  <span className="tabular-nums"><span className="sr-only">Roles here: </span>{removed.length}</span>
                </h2>
                <p className="flex h-7 items-center text-[12px] text-muted-foreground">Not in the funnel</p>
              </li>
            )}
          </Funnel>
          <div className={cn(grid, "flex-1")}>
            {STAGES.map((stage) => <Column key={stage.id} stage={stage} rows={items.filter((item) => item.stage === stage.id)} lit={lit} onOpen={onOpen} />)}
            {removed && <Column stage={REMOVED} rows={removed} lit={lit} onOpen={onOpen} />}
          </div>
        </div>
      </div>
      <DragOverlay dropAnimation={null}>
        {active && <div className={cn(ROLE_CARD, "cursor-grabbing shadow-lg ring-1 ring-violet-500/45")}><CardBody item={active} /></div>}
      </DragOverlay>
    </DndContext>
  )
}
