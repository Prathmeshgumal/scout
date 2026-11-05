import {
  DndContext, DragOverlay, KeyboardSensor, MouseSensor, TouchSensor, closestCenter, useDroppable, useSensor, useSensors,
  type Announcements, type CollisionDetection, type DragEndEvent, type DragOverEvent, type DragStartEvent,
} from "@dnd-kit/core"
import { SortableContext, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy } from "@dnd-kit/sortable"
import { CSS } from "@dnd-kit/utilities"
import { Archive } from "lucide-react"
import { useEffect, useMemo, useRef, useState } from "react"
import { VerdictBadge } from "@/components/jobs/JobRow"
import { ROLE_CARD, ROLE_CARD_ACTIVE, RoleCardBody } from "@/components/jobs/RoleCardBody"
import type { PipelineItem, PipelineStage, RemovedItem } from "@/lib/api"
import { isClosed } from "@/lib/jobs"
import { inOrder, moveCard, type Columns, type Positions } from "@/lib/board-order"
import { STAGES, funnelOf } from "@/lib/pipeline"
import { cn } from "@/lib/utils"
import { Funnel, StageIcon } from "./Funnel"

const COLUMNS = "grid grid-cols-[repeat(4,minmax(248px,1fr))] gap-2"
const COLUMNS_WITH_REMOVED = "grid grid-cols-[repeat(4,minmax(248px,1fr))_minmax(220px,0.8fr)] gap-2"
const REMOVED = { id: "removed", label: "Removed" } as const
type Card = PipelineItem | RemovedItem
type ColumnId = PipelineStage | "removed"
const stageIds: string[] = [...STAGES.map((stage) => stage.id), REMOVED.id]
const labelOf = (id: unknown) => [...STAGES, REMOVED].find((stage) => stage.id === id)?.label ?? ""

/**
 * The column comes from the pointer, left and right only, so a drop works anywhere in a column. In that column, the
 * nearest card gives the place. An empty column is the target itself.
 */
const inColumnOf = (columnOf: (id: string) => string | null): CollisionDetection => (args) => {
  const { droppableContainers, droppableRects, pointerCoordinates, collisionRect } = args
  const x = pointerCoordinates?.x ?? collisionRect.left + collisionRect.width / 2
  const columns = droppableContainers.filter((container) => stageIds.includes(String(container.id)))
  const column = columns
    .flatMap((container) => {
      const rect = droppableRects.get(container.id)
      return rect ? [{ container, distance: x < rect.left ? rect.left - x : x > rect.right ? x - rect.right : 0 }] : []
    })
    .sort((a, b) => a.distance - b.distance)[0]?.container
  if (!column) return []
  const cards = droppableContainers.filter((container) => columnOf(String(container.id)) === column.id)
  const nearest = cards.length ? closestCenter({ ...args, droppableContainers: cards }) : []
  return nearest.length ? nearest : [{ id: column.id, data: { droppableContainer: column, value: 0 } }]
}

const SCREEN_READER_HELP = { draggable: "To open this role, press Enter. To move it, press Space, choose a place with the arrow keys, and press Space again. A higher place is a higher priority." }

function CardBody({ item }: { item: Card }) {
  return <RoleCardBody company={item.company_name} title={item.title} location={item.location} noteCount={item.notes.length} closed={isClosed(item)} badge={item.fit && <VerdictBadge verdict={item.fit} />} />
}

function RoleCard({ item, onOpen, lit }: { item: Card; onOpen: () => void; lit: boolean }) {
  const { attributes, listeners, setNodeRef, isDragging, transform, transition } = useSortable({ id: item.role_key, data: { item }, attributes: { roleDescription: "draggable role" } })
  const node = useRef<HTMLButtonElement | null>(null)
  useEffect(() => {
    if (!lit || !node.current) return
    node.current.focus({ preventScroll: true })
    node.current.scrollIntoView({ block: "center", inline: "center" })
  }, [lit])
  return (
    <button ref={(el) => { node.current = el; setNodeRef(el) }} type="button" {...attributes} {...listeners} onClick={onOpen} data-highlighted={lit || undefined}
      style={{ transform: CSS.Translate.toString(transform), transition }}
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
      <SortableContext items={rows.map((item) => item.role_key)} strategy={verticalListSortingStrategy}>
        {rows.length === 0 ? <p className="px-2 py-3 text-[12.5px] text-muted-foreground">No roles</p> : (
          <ul className="flex flex-col gap-1.5">
            {rows.map((item) => <li key={item.role_key}><RoleCard item={item} lit={lit === item.role_key} onOpen={() => onOpen(item.role_key)} /></li>)}
          </ul>
        )}
      </SortableContext>
    </section>
  )
}

type Props = {
  items: PipelineItem[]
  /** The removed roles when the user shows their column. */
  removed: RemovedItem[] | null
  /** The card to show when the board opens. */
  highlight: string | null
  /** The places that the user gave to the cards. */
  positions: Positions
  /** A card was dropped: its column, and the new order of that column, top first. */
  onMove: (item: Card, stage: ColumnId, order: string[]) => void
  onOpen: (roleKey: string) => void
  /** The page stops reading new data while a card is in the air. */
  onDragging: (dragging: boolean) => void
}

/** The roles as columns, one column for each stage. A drop on another column changes the stage; a drop higher or lower changes the priority. */
export function PipelineBoard({ items, removed, highlight, positions, onMove, onOpen, onDragging }: Props) {
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
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates, keyboardCodes: { start: ["Space"], cancel: ["Escape"], end: ["Space", "Enter"] } }),
  )
  const cards = useMemo(() => new Map([...items, ...(removed ?? [])].map((item) => [item.role_key, item as Card])), [items, removed])
  // The order of each column from the data. While a card is in the air, the board shows its own copy.
  const base = useMemo<Columns>(() => {
    const columns: Columns = Object.fromEntries(STAGES.map((stage) => [stage.id, inOrder(items.filter((item) => item.stage === stage.id), (item) => item.role_key, stage.id, positions).map((item) => item.role_key)]))
    if (removed) columns.removed = inOrder(removed, (item) => item.role_key, REMOVED.id, positions).map((item) => item.role_key)
    return columns
  }, [items, removed, positions])
  const [dragged, setDragged] = useState<Columns | null>(null)
  const columns = dragged ?? base
  const columnOf = (id: string) => (stageIds.includes(id) ? id : Object.keys(columns).find((column) => columns[column].includes(id)) ?? null)
  const collision = inColumnOf(columnOf)
  const titleOf = (id: unknown) => [...items, ...(removed ?? [])].find((item) => item.role_key === id)?.title ?? "The role"
  const announcements: Announcements = {
    onDragStart: ({ active: card }) => `Picked up ${titleOf(card.id)}.`,
    onDragOver: ({ active: card, over }) => (over ? `${titleOf(card.id)} is in ${labelOf(columnOf(String(over.id)))}.` : undefined),
    onDragEnd: ({ active: card, over }) => (over ? `${titleOf(card.id)} moved to ${labelOf(columnOf(String(over.id)))}.` : `${titleOf(card.id)} stayed in its place.`),
    onDragCancel: ({ active: card }) => `${titleOf(card.id)} stayed in its place.`,
  }

  const start = ({ active: card }: DragStartEvent) => {
    setActive((card.data.current as { item: Card }).item)
    setDragged(base)
    onDragging(true)
  }
  const stop = () => {
    setActive(null)
    setDragged(null)
    onDragging(false)
  }
  // Over another column: the card goes there at once, so the cards of that column make room for it.
  const over = ({ active: card, over: target }: DragOverEvent) => {
    if (!target) return
    const key = String(card.id)
    const from = columnOf(key)
    const to = columnOf(String(target.id))
    if (!from || !to || from === to) return
    setDragged((now) => {
      const list = (now ?? base)[to] ?? []
      const at = stageIds.includes(String(target.id)) ? list.length : Math.max(0, list.indexOf(String(target.id)))
      return moveCard(now ?? base, key, to, at)
    })
  }
  const end = ({ active: card, over: target }: DragEndEvent) => {
    const item = (card.data.current as { item: Card }).item
    const key = String(card.id)
    const to = target ? columnOf(String(target.id)) : null
    let next = columns
    if (to && target && !stageIds.includes(String(target.id)) && target.id !== card.id) {
      next = moveCard(columns, key, to, columns[to].indexOf(String(target.id)))
    }
    stop()
    if (!to) return
    const order = next[to]
    const changed = to !== item.stage || order.join("|") !== (base[to] ?? []).join("|")
    if (changed) onMove(item, to as ColumnId, order)
  }

  const rowsOf = (id: ColumnId) => (columns[id] ?? []).flatMap((key) => { const card = cards.get(key); return card ? [card] : [] })
  const grid = removed ? COLUMNS_WITH_REMOVED : COLUMNS
  const steps = useMemo(() => funnelOf(items), [items])
  const counts = useMemo(() => Object.fromEntries(STAGES.map((stage) => [stage.id, items.filter((item) => item.stage === stage.id).length])), [items])
  return (
    <DndContext sensors={sensors} collisionDetection={collision} onDragStart={start} onDragOver={over} onDragEnd={end} onDragCancel={stop}
      accessibility={{ announcements, screenReaderInstructions: SCREEN_READER_HELP }}>
      <div className="flex min-h-0 flex-1">
        <div className="min-h-0 min-w-0 flex-1 overflow-auto px-4 pb-4">
          <div className={cn("flex min-h-full flex-col", removed ? "min-w-[1252px]" : "min-w-[1016px]")}>
            <div className={cn(grid, "sticky top-0 z-10 bg-background py-3")}>
              {STAGES.map((stage, i) => (
                <h2 key={stage.id} className="flex items-center gap-2 px-2 font-medium">
                  <StageIcon index={i} total={STAGES.length} />
                  <span className="truncate">{stage.label}</span>
                  <span className="text-muted-foreground tabular-nums"><span className="sr-only">Roles here: </span>{counts[stage.id] ?? 0}</span>
                </h2>
              ))}
              {removed && (
                <h2 className="flex items-center gap-2 px-2 font-medium text-muted-foreground">
                  <Archive className="size-3.5 shrink-0" aria-hidden />
                  <span className="truncate">{REMOVED.label}</span>
                  <span className="tabular-nums"><span className="sr-only">Roles here: </span>{removed.length}</span>
                </h2>
              )}
            </div>
            <div className={cn(grid, "flex-1")}>
              {STAGES.map((stage) => <Column key={stage.id} stage={stage} rows={rowsOf(stage.id)} lit={lit} onOpen={onOpen} />)}
              {removed && <Column stage={REMOVED} rows={rowsOf(REMOVED.id)} lit={lit} onOpen={onOpen} />}
            </div>
          </div>
        </div>
        <aside className="hidden w-[380px] shrink-0 overflow-y-auto border-l p-6 lg:block">
          <Funnel steps={steps} />
        </aside>
      </div>
      <DragOverlay dropAnimation={null}>
        {active && <div className={cn(ROLE_CARD, "cursor-grabbing shadow-lg ring-1 ring-violet-500/45")}><CardBody item={active} /></div>}
      </DragOverlay>
    </DndContext>
  )
}
