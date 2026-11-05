import { PanelLeftOpen } from "lucide-react"
import { useEffect, useRef, useState, type ReactNode } from "react"
import { Button } from "@/components/ui/button"
import { NAMES, type Mail, type MapState } from "@/lib/system-map"
import { cn } from "@/lib/utils"
import { infoFor } from "./info"
import { SystemMap, type MapStyle } from "./SystemMap"

export const STYLE_KEY = "job-finder-activity-map-style"

function readStyle(): MapStyle {
  try {
    return localStorage.getItem(STYLE_KEY) === "blueprint" ? "blueprint" : "pixel"
  } catch {
    return "pixel"
  }
}

function saveStyle(style: MapStyle) {
  try {
    localStorage.setItem(STYLE_KEY, style)
  } catch {
    // The choice then lasts only while the page is open.
  }
}

/** Two or more buttons, one pressed. */
export function Segmented<T extends string>({ label, value, options, onChange }: { label: string; value: T; options: [T, ReactNode][]; onChange: (v: T) => void }) {
  return (
    <div role="group" aria-label={label} className="flex shrink-0 gap-0.5 rounded-md border bg-muted p-0.5">
      {options.map(([v, text]) => (
        <button key={v} type="button" aria-pressed={value === v} onClick={() => onChange(v)}
          className={cn("h-6 rounded px-2.5 text-[12px] outline-none focus-visible:ring-2 focus-visible:ring-ring",
            value === v ? "bg-background text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground")}>
          {text}
        </button>
      ))}
    </div>
  )
}

const who = (name: string) => (name === "career-lens" || name === "gate" ? (name === "gate" ? "Gate" : name) : NAMES[name] ?? name)
const KIND_CLASS: Record<Mail["kind"], string> = {
  request: "bg-amber-100 text-amber-900 dark:bg-amber-400/15 dark:text-amber-200",
  result: "bg-emerald-100 text-emerald-900 dark:bg-emerald-400/15 dark:text-emerald-200",
  refused: "bg-red-100 text-red-900 dark:bg-red-400/15 dark:text-red-200",
}

type Props = {
  agents: string[]
  state: MapState
  maxSpend?: number | null
  crumb: ReactNode
  toolbar: ReactNode
  onShowTree?: () => void
}

/** The map, what the selected part is, and the requests and results so far. */
export function MapPane({ agents, state, maxSpend, crumb, toolbar, onShowTree }: Props) {
  const [look, setLook] = useState(readStyle)
  const [selected, setSelected] = useState("queue")
  const [width, setWidth] = useState(0)
  const pane = useRef<HTMLElement>(null)

  useEffect(() => {
    const el = pane.current
    if (!el) return
    const fit = () => setWidth(Math.max(320, Math.min(el.clientWidth - 32, ((el.clientHeight - 210) * 1000) / 600)))
    fit()
    if (typeof ResizeObserver === "undefined") return
    const observer = new ResizeObserver(fit)
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  const changeLook = (v: MapStyle) => {
    setLook(v)
    saveStyle(v)
  }
  const info = infoFor(selected)
  const mail = state.mail.slice(-100).reverse()

  return (
    <section ref={pane} aria-label="System map" className="flex min-w-0 flex-1 flex-col gap-2 overflow-y-auto p-4">
      <div className="flex items-center gap-2">
        {onShowTree && (
          <Button variant="outline" size="icon" className="size-7 shrink-0" aria-label="Show the step list" title="Show the step list" onClick={onShowTree}>
            <PanelLeftOpen aria-hidden />
          </Button>
        )}
        <p className="min-w-0 flex-1 truncate text-[12px] text-muted-foreground">{crumb}</p>
        <Segmented label="Map style" value={look} onChange={changeLook} options={[["pixel", "Pixel"], ["blueprint", "Blueprint"]]} />
        {toolbar}
      </div>
      <div className="sysmap mx-auto" style={{ width, height: (width * 600) / 1000 }}>
        {width > 0 && <SystemMap agents={agents} state={state} look={look} selected={selected} onSelect={setSelected} maxSpend={maxSpend} scale={width / 1000} />}
      </div>
      <p className="text-center text-[11px] text-muted-foreground">Click a part of the map to see what it does.</p>
      <div className="grid gap-2 lg:grid-cols-[1.1fr_1fr]">
        <div className="rounded-lg border bg-card">
          <h4 className="flex justify-between gap-2 border-b px-3 py-1.5 text-[12px] font-medium">
            <span>{info.title}</span><span className="font-normal text-muted-foreground">{info.area}</span>
          </h4>
          <div className="space-y-1.5 px-3 py-2 text-[12px]">
            {info.text.map((p) => <p key={p}>{p}</p>)}
            <p className="font-mono text-[11px] text-muted-foreground">{info.tech}</p>
          </div>
        </div>
        <div className="flex min-h-0 flex-col rounded-lg border bg-card">
          <h4 className="flex justify-between gap-2 border-b px-3 py-1.5 text-[12px] font-medium">
            <span>Requests and results</span><span className="font-normal text-muted-foreground">{state.mail.length} so far</span>
          </h4>
          <ul className="max-h-40 overflow-y-auto text-[12px]" aria-label="Requests and results">
            {mail.length ? mail.map((m, i) => (
              <li key={`${m.at}-${i}`} className="border-b px-3 py-1.5 last:border-b-0">
                <span className={cn("mr-1.5 rounded px-1.5 text-[10.5px]", KIND_CLASS[m.kind])}>{m.kind}</span>
                <span className="text-muted-foreground">{who(m.from)} → {who(m.to)}</span>
                <p className="break-words">{m.to === "career-lens" ? <code className="font-mono text-[11px]">{m.text}</code> : m.text}</p>
              </li>
            )) : <li className="px-3 py-1.5 text-muted-foreground">Nothing sent yet.</li>}
          </ul>
        </div>
      </div>
    </section>
  )
}
