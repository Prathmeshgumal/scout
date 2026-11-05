import { ChevronLeft, ChevronRight, Copy, Check, Download, PanelLeftClose, PanelLeftOpen, Search } from "lucide-react"
import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent, type ReactNode } from "react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { download, eventLogPath, getStep, type GoalEvent, type GoalStatus, type StepDetail } from "@/lib/api"
import { buildActivity, firstStep, matches, nextStep, type Activity, type Filter, type Run, type Step } from "@/lib/activity"
import { cn } from "@/lib/utils"
import { AgentDot, StateIcon, STATE_WORD, agentColor, duration } from "./agents"

export const TREE_KEY = "job-finder-activity-tree"
export const LANES_KEY = "job-finder-activity-lanes"
/** The height of the lanes in pixels. The smallest height shows the time line and one lane. */
const LANES_MIN = 64
const LANES_MAX = 520
const LANES_DEFAULT = 144
const LANES_STEP = 20

const clamp = (value: number) => Math.min(LANES_MAX, Math.max(LANES_MIN, Math.round(value)))

function readLanes(): number {
  try {
    const value = Number(localStorage.getItem(LANES_KEY))
    return value ? clamp(value) : LANES_DEFAULT
  } catch {
    return LANES_DEFAULT
  }
}

function saveLanes(height: number) {
  try {
    localStorage.setItem(LANES_KEY, String(height))
  } catch {
    // The height then lasts only while the page is open.
  }
}

function readTree(): boolean {
  try {
    return localStorage.getItem(TREE_KEY) !== "hidden"
  } catch {
    return true
  }
}

function saveTree(shown: boolean) {
  try {
    localStorage.setItem(TREE_KEY, shown ? "shown" : "hidden")
  } catch {
    // The choice then lasts only while the page is open.
  }
}

/** The time now. It changes each second while the goal runs, so that the lanes grow. */
function useNow(running: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!running) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [running])
  return now
}

const clock = (ms: number) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })

type Props = { goalId: string; events: GoalEvent[]; status: GoalStatus; initialStep?: string | null }

/** All the work of a goal: a step list, the detail of one step, and one lane for each agent. */
export function ActivityTab({ goalId, events, status, initialStep }: Props) {
  const running = status === "running"
  const now = useNow(running)
  const activity = useMemo(() => buildActivity(events, status, now), [events, status, now])
  const [chosen, setChosen] = useState<string | null>(initialStep ?? null)
  const [filter, setFilter] = useState<Filter>({ query: "", kind: "all" })
  const [treeShown, setTreeShown] = useState(readTree)
  const [lanesHeight, setLanesHeight] = useState(readLanes)

  useEffect(() => {
    if (initialStep) setChosen(initialStep)
  }, [initialStep])

  const selectedId = chosen && activity.steps.some((s) => s.id === chosen) ? chosen : firstStep(activity.steps)
  const selected = activity.steps.find((s) => s.id === selectedId) ?? null

  const toggleTree = () => {
    setTreeShown((shown) => {
      saveTree(!shown)
      return !shown
    })
  }
  const setKind = (kind: Filter["kind"]) => setFilter((f) => ({ ...f, kind }))
  const counts: Record<Filter["kind"], number> = {
    all: activity.steps.length,
    problems: activity.steps.filter((s) => s.state === "failed").length,
    saves: activity.steps.filter((s) => s.saved).length,
  }
  const resize = (height: number, keep: boolean) => {
    const next = clamp(height)
    setLanesHeight(next)
    if (keep) saveLanes(next)
  }
  const saveLog = () => download(eventLogPath(goalId)).catch((e: unknown) => toast.error(e instanceof Error ? e.message : "The log did not download."))

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b px-3 py-2">
        <div className="relative w-56">
          <Search aria-hidden className="pointer-events-none absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input aria-label="Search the steps" placeholder="Search the steps" value={filter.query}
            onChange={(e) => setFilter((f) => ({ ...f, query: e.target.value }))} className="h-7 pl-7 text-[12px]" />
        </div>
        <div role="group" aria-label="Show" className="flex gap-1">
          {(["all", "problems", "saves"] as const).map((kind) => (
            <Chip key={kind} pressed={filter.kind === kind} onClick={() => setKind(kind)}>
              {kind === "all" ? "All" : kind === "problems" ? "Problems" : "Saves"}{" "}
              <span className="tabular-nums opacity-70">{counts[kind]}</span>
            </Chip>
          ))}
        </div>
        <div className="flex-1" />
        <Button variant="outline" size="sm" className="h-7 text-[12px]" onClick={() => void saveLog()}>
          <Download aria-hidden /> Download log
        </Button>
      </div>
      {activity.steps.length === 0 ? (
        <p className="m-auto px-6 text-center text-muted-foreground">No steps yet. Steps show here when the agents start to work.</p>
      ) : (
        <>
          <div className="flex min-h-0 flex-1">
            {treeShown && (
              <aside aria-label="Step list" className="flex w-72 shrink-0 flex-col border-r">
                <div className="flex h-9 shrink-0 items-center justify-between border-b pr-1 pl-3 text-[12px]">
                  <span className="font-medium">Steps</span>
                  <Button variant="ghost" size="icon" className="size-7" aria-label="Hide the step list" title="Hide the step list" onClick={toggleTree}>
                    <PanelLeftClose aria-hidden />
                  </Button>
                </div>
                <StepTree activity={activity} filter={filter} selectedId={selectedId} onSelect={setChosen} />
              </aside>
            )}
            <StepPane goalId={goalId} activity={activity} step={selected} onSelect={setChosen} onShowTree={treeShown ? undefined : toggleTree} />
          </div>
          <LanesHandle height={lanesHeight} onResize={resize} />
          <Lanes activity={activity} filter={filter} selectedId={selectedId} running={running} height={lanesHeight} onSelect={setChosen} />
        </>
      )}
    </div>
  )
}

function Chip({ pressed, onClick, children }: { pressed: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button type="button" aria-pressed={pressed} onClick={onClick}
      className={cn("h-7 rounded-full border px-3 text-[12px] outline-none focus-visible:ring-2 focus-visible:ring-ring",
        pressed ? "border-foreground/30 bg-foreground text-background" : "text-muted-foreground hover:text-foreground")}>
      {children}
    </button>
  )
}

/** A tree row: a step of the main agent, or a run with the steps that match the filter. */
type Shown = { kind: "step"; step: Step } | { kind: "run"; run: Run; steps: Step[] }

function StepTree({ activity, filter, selectedId, onSelect }: { activity: Activity; filter: Filter; selectedId: string | null; onSelect: (id: string) => void }) {
  const filtered = filter.kind !== "all" || filter.query.trim() !== ""
  const selectedRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    selectedRef.current?.scrollIntoView?.({ block: "nearest" })
  }, [selectedId])

  const stepItem = (step: Step, nested: boolean) => (
    <div key={step.id} ref={step.id === selectedId ? selectedRef : undefined} role="treeitem" aria-selected={step.id === selectedId}
      aria-label={`${step.label}, ${STATE_WORD[step.state]}`} data-step={step.id} tabIndex={step.id === selectedId ? 0 : -1}
      onClick={() => onSelect(step.id)} onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onSelect(step.id)}
      className={cn("flex cursor-pointer items-center gap-1.5 rounded-md py-1 pr-2 outline-none focus-visible:ring-2 focus-visible:ring-ring",
        nested ? "pl-6" : "pl-2", step.id === selectedId ? "bg-muted text-foreground" : "text-foreground/80 hover:bg-muted/60")}>
      <StateIcon state={step.state} className="size-3" />
      {!nested && <AgentDot agent={step.agent} />}
      <span className="min-w-0 flex-1 truncate">{step.label}</span>
      {step.endedAt !== null && <span className="shrink-0 text-[11px] text-muted-foreground tabular-nums">{duration(step.endedAt - step.startedAt)}</span>}
    </div>
  )

  return (
    <div role="tree" aria-label="Steps" className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-2 text-[12px]">
      {activity.answers.map((answer) => {
        const items = answer.items.flatMap((item): Shown[] => {
          if (item.kind === "step") return matches(item.step, filter) ? [item] : []
          const steps = item.run.steps.filter((s) => matches(s, filter))
          return steps.length || !filtered ? [{ ...item, steps }] : []
        })
        if (filtered && !items.length) return null
        return (
          <div key={answer.n} role="treeitem" aria-expanded aria-label={`Message ${answer.n}: ${answer.text || "Start"}`} aria-selected={false}>
            <p className="truncate px-2 pb-1 font-medium text-muted-foreground" title={answer.text}>Message {answer.n}{answer.text ? `: ${answer.text}` : ""}</p>
            <div role="group" className="flex flex-col gap-0.5">
              {items.map((item) =>
                item.kind === "step" ? stepItem(item.step, false) : (
                  <div key={item.run.id} role="treeitem" aria-expanded aria-selected={false} aria-label={`${item.run.agent}, ${STATE_WORD[item.run.state]}`}>
                    <p className="flex items-center gap-1.5 px-2 py-1">
                      <StateIcon state={item.run.state} className="size-3" />
                      <AgentDot agent={item.run.agent} />
                      <span className="min-w-0 flex-1 truncate font-medium">{item.run.agent}</span>
                      <span className="shrink-0 text-[11px] text-muted-foreground">{item.run.steps.length} {item.run.steps.length === 1 ? "step" : "steps"}</span>
                    </p>
                    <div role="group" className="flex flex-col gap-0.5">{item.steps.map((s) => stepItem(s, true))}</div>
                  </div>
                ),
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}

type Loaded = { id: string; detail: StepDetail | null; error: string | null }

type PaneProps = { goalId: string; activity: Activity; step: Step | null; onSelect: (id: string) => void; onShowTree?: () => void }

function StepPane({ goalId, activity, step, onSelect, onShowTree }: PaneProps) {
  const [loaded, setLoaded] = useState<Loaded | null>(null)
  const id = step?.id ?? null
  const state = step?.state
  useEffect(() => {
    if (!id) return
    let live = true
    getStep(goalId, id)
      .then((detail) => live && setLoaded({ id, detail, error: null }))
      .catch((e: unknown) => live && setLoaded({ id, detail: null, error: e instanceof Error ? e.message : "The step did not load." }))
    return () => {
      live = false
    }
  }, [goalId, id, state])

  const showTree = onShowTree && (
    <Button variant="outline" size="icon" className="size-7 shrink-0" aria-label="Show the step list" title="Show the step list" onClick={onShowTree}>
      <PanelLeftOpen aria-hidden />
    </Button>
  )
  if (!step) return <section aria-label="Step detail" className="flex-1 p-4">{showTree}</section>
  const answer = activity.answers.find((a) => a.items.some((i) => (i.kind === "step" ? i.step.id === step.id : i.run.steps.some((s) => s.id === step.id))))
  const before = nextStep(activity.steps, step.id, -1)
  const after = nextStep(activity.steps, step.id, 1)
  const current = loaded?.id === step.id ? loaded : null

  return (
    <section aria-label="Step detail" className="flex min-w-0 flex-1 flex-col gap-3 overflow-y-auto p-4">
      <div className="flex items-start gap-2">
        {showTree}
        <div className="min-w-0 flex-1">
          <p className="truncate text-[12px] text-muted-foreground">
            {answer ? `Message ${answer.n}` : "Goal"} › {step.runId ? step.agent : "main agent"}
          </p>
          <h3 className="mt-0.5 break-words text-[15px] font-medium">{step.label}</h3>
        </div>
        <span className={cn("inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 text-[11px]",
          step.state === "failed" && "border-red-500/40 text-red-700 dark:text-red-300")}>
          <StateIcon state={step.state} className="size-3" /> {STATE_WORD[step.state]}
        </span>
        <div className="flex shrink-0 gap-1">
          <Button variant="outline" size="icon" className="size-7" aria-label="Previous step" disabled={!before} onClick={() => before && onSelect(before)}><ChevronLeft /></Button>
          <Button variant="outline" size="icon" className="size-7" aria-label="Next step" disabled={!after} onClick={() => after && onSelect(after)}><ChevronRight /></Button>
        </div>
      </div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[12px] sm:grid-cols-[auto_1fr_auto_1fr]">
        <dt className="text-muted-foreground">Agent</dt>
        <dd className="flex items-center gap-1.5"><AgentDot agent={step.agent} /> {step.agent}</dd>
        <dt className="text-muted-foreground">Tool</dt>
        <dd className="font-mono text-[11px]">{step.tool}</dd>
        <dt className="text-muted-foreground">Started</dt>
        <dd className="tabular-nums">{clock(step.startedAt)}</dd>
        <dt className="text-muted-foreground">Took</dt>
        <dd className="tabular-nums">{step.endedAt === null ? (step.state === "running" ? "Still working" : "Not known") : duration(step.endedAt - step.startedAt)}</dd>
      </dl>
      {step.summary && (
        <p className={cn("rounded-md px-3 py-2 text-[12px] break-words", step.state === "failed" ? "bg-red-500/10 text-red-800 dark:text-red-200" : "bg-muted")}>{step.summary}</p>
      )}
      {!current ? (
        <p className="text-[12px] text-muted-foreground">Loading the input and output…</p>
      ) : current.error ? (
        <p className="text-[12px] text-red-700 dark:text-red-300">{current.error}</p>
      ) : current.detail && !current.detail.kept ? (
        <p className="rounded-md border border-dashed px-3 py-2 text-[12px] text-muted-foreground">Not kept: this step can hold resume text, so the log does not keep its input and output.</p>
      ) : current.detail && current.detail.input == null && current.detail.output == null ? (
        <p className="rounded-md border border-dashed px-3 py-2 text-[12px] text-muted-foreground">
          {step.state === "running" ? "The output shows here when the step finishes." : "Not recorded: this step ran before the log kept its input and output."}
        </p>
      ) : current.detail ? (
        <>
          {current.detail.truncated && <p className="text-[12px] text-muted-foreground">Long values are cut at 20,000 characters.</p>}
          <Value title="Input" text={current.detail.input == null ? null : typeof current.detail.input === "string" ? current.detail.input : JSON.stringify(current.detail.input, null, 2)} />
          <Value title="Output" text={current.detail.output} empty={step.state === "running" ? "The output shows here when the step finishes." : "No output."} />
        </>
      ) : null}
    </section>
  )
}

function Value({ title, text, empty = "No input." }: { title: string; text: string | null; empty?: string }) {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    if (!text) return
    navigator.clipboard?.writeText(text).then(() => {
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    }, () => toast.error("The text did not copy."))
  }
  return (
    <div className="flex min-h-0 flex-col rounded-md border">
      <div className="flex items-center justify-between border-b px-3 py-1">
        <h4 className="text-[12px] font-medium">{title}</h4>
        {text && (
          <Button variant="ghost" size="sm" className="h-6 text-[11px]" onClick={copy} aria-label={`Copy the ${title.toLowerCase()}`}>
            {copied ? <Check aria-hidden /> : <Copy aria-hidden />} {copied ? "Copied" : "Copy"}
          </Button>
        )}
      </div>
      {text ? (
        <pre className="max-h-80 overflow-auto px-3 py-2 font-mono text-[11px] leading-relaxed break-words whitespace-pre-wrap">{text}</pre>
      ) : (
        <p className="px-3 py-2 text-[12px] text-muted-foreground">{empty}</p>
      )}
    </div>
  )
}

const LANE_LABEL = 128

/** The bar above the lanes. Drag it, or use the arrow keys, to change the height of the lanes. */
function LanesHandle({ height, onResize }: { height: number; onResize: (height: number, keep: boolean) => void }) {
  const drag = useRef<{ y: number; height: number } | null>(null)
  const down = (e: PointerEvent<HTMLDivElement>) => {
    drag.current = { y: e.clientY, height }
    e.currentTarget.setPointerCapture?.(e.pointerId)
  }
  const move = (e: PointerEvent<HTMLDivElement>) => {
    if (drag.current) onResize(drag.current.height + drag.current.y - e.clientY, false)
  }
  const up = (e: PointerEvent<HTMLDivElement>) => {
    if (!drag.current) return
    onResize(drag.current.height + drag.current.y - e.clientY, true)
    drag.current = null
  }
  const key = (e: KeyboardEvent<HTMLDivElement>) => {
    const change = { ArrowUp: LANES_STEP, ArrowDown: -LANES_STEP, Home: LANES_MAX, End: -LANES_MAX }[e.key]
    if (change === undefined) return
    e.preventDefault()
    onResize(height + change, true)
  }
  return (
    <div role="separator" aria-orientation="horizontal" aria-label="Change the height of the lanes" tabIndex={0}
      aria-valuenow={height} aria-valuemin={LANES_MIN} aria-valuemax={LANES_MAX}
      onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={up} onKeyDown={key}
      className="group relative h-2 shrink-0 cursor-row-resize touch-none border-t outline-none focus-visible:bg-ring/30 hover:bg-muted">
      <span aria-hidden className="absolute top-1/2 left-1/2 h-1 w-10 -translate-x-1/2 -translate-y-1/2 rounded-full bg-border group-hover:bg-muted-foreground/50" />
    </div>
  )
}

type LanesProps = { activity: Activity; filter: Filter; selectedId: string | null; running: boolean; height: number; onSelect: (id: string) => void }

function Lanes({ activity, filter, selectedId, running, height, onSelect }: LanesProps) {
  const span = Math.max(activity.end - activity.start, 1000)
  const pos = (t: number) => ((t - activity.start) / span) * 100
  return (
    <section aria-label="Agent lanes" style={{ height }} className="flex shrink-0 flex-col px-3 pb-2 text-[11px]">
      <div className="flex items-center justify-between pb-1 text-muted-foreground" style={{ paddingLeft: LANE_LABEL }}>
        <span className="tabular-nums">{clock(activity.start)}</span>
        <span className="tabular-nums">{running ? "Now" : clock(activity.end)}</span>
      </div>
      <div className="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto">
        {activity.agents.map((agent) => (
          <div key={agent} className="flex items-center">
            <span className="flex shrink-0 items-center gap-1.5 truncate pr-2" style={{ width: LANE_LABEL }}>
              <AgentDot agent={agent} /> <span className="truncate">{agent}</span>
            </span>
            <div className="relative h-4 flex-1 rounded-sm bg-muted/50">
              {activity.steps.filter((s) => s.agent === agent).map((step) => {
                const end = step.endedAt ?? (step.state === "running" ? activity.end : step.startedAt)
                const left = pos(step.startedAt)
                const width = Math.max(pos(end) - left, 0.6)
                const dim = !matches(step, filter)
                return (
                  <button key={step.id} type="button" onClick={() => onSelect(step.id)} title={`${step.label} (${STATE_WORD[step.state]})`}
                    aria-label={`${step.label}, ${agent}, ${STATE_WORD[step.state]}`} aria-pressed={step.id === selectedId}
                    style={{ left: `${left}%`, width: `${Math.min(width, 100 - left)}%` }}
                    className={cn("absolute inset-y-0.5 min-w-1 rounded-[3px] outline-none focus-visible:ring-2 focus-visible:ring-ring",
                      agentColor(step.agent), dim ? "opacity-20" : "opacity-80 hover:opacity-100",
                      step.state === "failed" && "ring-2 ring-red-600 dark:ring-red-400",
                      step.id === selectedId && "opacity-100 ring-2 ring-foreground ring-offset-1 ring-offset-background")} />
                )
              })}
              {running && <span aria-hidden className="absolute inset-y-[-2px] right-0 w-px bg-foreground/60" />}
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}
