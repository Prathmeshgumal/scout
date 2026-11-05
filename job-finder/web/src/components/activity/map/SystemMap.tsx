import { memo, type CSSProperties, type ReactNode } from "react"
import { NAMES, type MapState, type Mover } from "@/lib/system-map"
import { cn } from "@/lib/utils"
import { infoFor } from "./info"
import "./map.css"

export type MapStyle = "pixel" | "blueprint"

/** Places on the map, in map pixels. The map is 1000 x 600. */
const P: Record<string, [number, number]> = {
  claude: [215, 30], board: [224, 112], jar: [52, 248], main: [160, 250], reader: [330, 250], archive: [224, 535], gate: [441, 320],
  dispatcher: [540, 170], queue: [672, 170], history: [686, 420], google: [885, 150], linkedin: [885, 320], careers: [885, 490],
}
const WORKERS: [number, number][] = [[552, 300], [552, 410], [552, 520]]
const ROW_Y = 392

/** The subagents stand in one row under Scout. */
function rowPlaces(agents: string[]): Record<string, [number, number]> {
  const subs = agents.filter((a) => a !== "main")
  const places: Record<string, [number, number]> = { main: P.main }
  subs.forEach((a, i) => {
    places[a] = [subs.length === 1 ? 222 : 52 + (i * 340) / (subs.length - 1), ROW_Y]
  })
  return places
}

type Look = { hair: string; shirt: string; skin: string }
const LOOKS: Record<string, Look> = {
  main: { hair: "#2b1a10", shirt: "#52525b", skin: "#e0ac7e" },
  "candidate-profile": { hair: "#4a2511", shirt: "#0369a1", skin: "#f1c27d" },
  "job-discovery": { hair: "#1c1917", shirt: "#7c3aed", skin: "#c68a5a" },
  "job-research": { hair: "#7c4a1e", shirt: "#b45309", skin: "#f1c27d" },
  "fit-check": { hair: "#0f0f0f", shirt: "#0f766e", skin: "#8d5524" },
  "application-prep": { hair: "#3f2a14", shirt: "#be185d", skin: "#e8b48a" },
  other: { hair: "#44403c", shirt: "#4338ca", skin: "#d6a77a" },
  reader: { hair: "#d4d4d8", shirt: "#3f3f46", skin: "#e0ac7e" },
  dispatcher: { hair: "#57534e", shirt: "#0369a1", skin: "#c68a5a" },
}
const ROWS = ["...kkkkkk...", "..khhhhhhk..", ".khhhhhhhhk.", ".khsssssshk.", ".kssesssesk.", ".kssssssssk.", "..kssmmssk..", "...kssssk...",
  "..kcccccck..", ".kcccccccck.", "ksccccccccsk", "ks.cccccc.sk", "...pppppp...", "...pp..pp...", "...kk..kk..."]

const Sprite = memo(function Sprite({ look }: { look: Look }) {
  const pal: Record<string, string> = { k: "#0c0a09", h: look.hair, s: look.skin, e: "#0c0a09", m: "#7f1d1d", c: look.shirt, p: "#27272a" }
  return (
    <svg width={36} height={45} viewBox="0 0 12 15" shapeRendering="crispEdges" aria-hidden>
      {ROWS.flatMap((row, y) => [...row].map((ch, x) => (pal[ch] ? <rect key={`${x}-${y}`} x={x} y={y} width={1} height={1} fill={pal[ch]} /> : null)))}
    </svg>
  )
})

function Envelope({ seal, refused }: { seal: string; refused?: boolean }) {
  return (
    <svg width={22} height={16} viewBox="0 0 13 9" shapeRendering="crispEdges" aria-hidden>
      <rect width={13} height={9} fill="#0c0a09" />
      <rect x={1} y={1} width={11} height={7} fill={refused ? "#fecaca" : "#fef3c7"} />
      <path d="M1 1h1v1h1v1h1v1h1v1h3v-1h1v-1h1v-1h1v-1h1v1h-1v1h-1v1h-1v1h-1v1h-3v-1h-1v-1h-1v-1h-1v-1h-1z" fill="#d6c7a1" />
      <rect x={6} y={4} width={1} height={1} fill={seal} />
    </svg>
  )
}

function Page() {
  return (
    <svg width={16} height={20} viewBox="0 0 8 10" shapeRendering="crispEdges" aria-hidden>
      <rect width={8} height={10} fill="#0c0a09" />
      <rect x={1} y={1} width={6} height={8} fill="#fff" />
      <rect x={2} y={2} width={4} height={1} fill="#a1a1aa" />
      <rect x={2} y={4} width={4} height={1} fill="#a1a1aa" />
      <rect x={2} y={6} width={3} height={1} fill="#a1a1aa" />
    </svg>
  )
}

const KNOWN = new Set(Object.keys(NAMES))
export const agentVar = (agent: string) => (KNOWN.has(agent) && agent !== "gate" && agent !== "career-lens" ? `var(--a-${agent})` : "var(--a-other)")
const nameOf = (agent: string) => NAMES[agent] ?? agent

/** A point along a path of points. f: 0 at the start, 1 at the end. */
function along(points: [number, number][], f: number): [number, number] {
  const lengths = points.slice(1).map((p, i) => Math.hypot(p[0] - points[i][0], p[1] - points[i][1]))
  let rest = f * lengths.reduce((a, b) => a + b, 0)
  for (let i = 0; i < lengths.length; i++) {
    if (rest <= lengths[i] || i === lengths.length - 1) {
      const p = lengths[i] ? Math.min(1, rest / lengths[i]) : 1
      return [points[i][0] + (points[i + 1][0] - points[i][0]) * p, points[i][1] + (points[i + 1][1] - points[i][1]) * p]
    }
    rest -= lengths[i]
  }
  return points[points.length - 1]
}

const up = ([x, y]: [number, number], d = 34): [number, number] => [x, y - d]

type Props = {
  agents: string[]
  state: MapState
  look: MapStyle
  selected: string
  onSelect: (id: string) => void
  maxSpend?: number | null
  scale: number
}

export function SystemMap({ agents, state, look, selected, onSelect, maxSpend, scale }: Props) {
  const places = rowPlaces(agents)
  const subs = agents.filter((a) => a !== "main")
  const bubbleWidth = Math.min(118, Math.max(70, 340 / Math.max(subs.length - 1, 1) - 6))

  const place = (name: string, m: Mover): [number, number] => {
    if (name === "agent") return up(places[m.agent] ?? places.main)
    if (name.startsWith("agent:")) return up(places[name.slice(6)] ?? places.main)
    if (name === "worker") return WORKERS[Math.min(m.worker ?? 0, WORKERS.length - 1)]
    if (name === "site") return P[m.site ?? "careers"]
    if (name === "reader") return up(P.reader)
    if (name === "dispatcher" || name === "gate") return up(P[name], 20)
    return P[name]
  }

  const node = (id: string, at: [number, number], children: ReactNode, extra?: { className?: string; color?: string; info?: string }) => (
    <button key={id} type="button" data-node={id} aria-label={infoFor(extra?.info ?? id).title} aria-pressed={selected === (extra?.info ?? id)}
      onClick={() => onSelect(extra?.info ?? id)}
      className={cn("node", extra?.className, selected === (extra?.info ?? id) && "sel")}
      style={{ left: at[0], top: at[1], ...(extra?.color ? { "--c": extra.color } : {}) } as CSSProperties}>
      {children}
    </button>
  )
  const bp = (title: string, text: string, on = false) => <div className={cn("bpf", on && "on")}><b>{title}</b><small>{text}</small></div>

  const cap = maxSpend && maxSpend > 0 ? maxSpend : null
  const fill = cap ? Math.min(1, state.spend / cap) : 0
  const money = `$${state.spend.toFixed(2)}`
  const columns: [string, number, boolean][] = [["Found", state.board.found, false], ["Researched", state.board.researched, false],
    ["Checked", state.board.checked, false], ["Fit", state.board.fit, true]]
  const shown = state.workers.slice(0, WORKERS.length)
  const more = state.workers.length - shown.length
  const liSign = state.linkedin === "open" ? "In use now" : state.linkedin === "used" ? "Used in this goal" : "Not used in this goal"
  const edges: [string, string, string][] = [["claude", "main", "Messages API"], ["main", "reader", ""], ["main", "jar", "cost"],
    ["gate", "dispatcher", "HTTPS /v1 + key"], ["dispatcher", "queue", "ticket"], ["dispatcher", "history", "fresh copy?"],
    ["queue", "w0", ""], ["w0", "google", "Chrome"], ["w0", "linkedin", ""], ["w1", "careers", ""], ["w0", "history", "save page"]]
  const at = (id: string) => (id === "w0" ? WORKERS[0] : id === "w1" ? WORKERS[1] : P[id])

  return (
    <div className={cn("map", look === "blueprint" && "bp")} style={{ transform: `scale(${scale})` }} role="group" aria-label="System map">
      <div className="zone z-hq"><span className="zt">job-finder<small>agents · guardrails · your data</small></span></div>
      <div className="zone z-depot"><span className="zt">career-lens<small>your Chrome workers</small></span></div>
      <div className="zone z-web"><span className="zt">The web</span></div>
      <div className="road" style={{ left: 444, top: 312, width: 30, height: 18 }} />
      <div className="road" style={{ left: 620, top: 322, width: 162, height: 14 }} />
      <div className="road" style={{ left: 620, top: 482, width: 162, height: 14 }} />
      <svg className="edges" width={1000} height={600} aria-hidden>
        <defs>
          <marker id="sysmap-arrow" viewBox="0 0 8 8" refX={7} refY={4} markerWidth={7} markerHeight={7} orient="auto">
            <path d="M0 0L8 4L0 8z" fill="var(--bp-line)" />
          </marker>
        </defs>
        <rect className="e" x={22} y={330} width={404} height={112} strokeDasharray="4 3" />
        <text className="el" x={30} y={456}>agent team (Claude Agent SDK) → tool calls → gate</text>
        <path className="e" d="M426 386 H441" markerEnd="url(#sysmap-arrow)" />
        <path className="e" d="M224 442 L224 515" markerEnd="url(#sysmap-arrow)" />
        <text className="el" x={230} y={485}>SQL</text>
        {edges.map(([a, b, label]) => {
          const [x1, y1] = at(a)
          const [x2, y2] = at(b)
          return (
            <g key={`${a}-${b}`}>
              <path className="e" d={`M${x1} ${y1} L${x2} ${y2}`} markerEnd="url(#sysmap-arrow)" />
              {label && <text className="el" x={(x1 + x2) / 2 + 4} y={(y1 + y2) / 2 - 4}>{label}</text>}
            </g>
          )
        })}
      </svg>

      {node("claude", P.claude, <>
        <div className="px"><div className={cn("cloud", state.claude && "busy")}>{state.claude ? "Claude · thinking" : "Claude"}</div></div>
        {bp("Claude", state.claude ? "thinking" : "Anthropic API", state.claude)}
      </>)}
      {node("board", P.board, <>
        <div className="px">
          <div className="board">
            {columns.map(([name, n, fit]) => (
              <div key={name} className="col">
                <b>{name} {n}</b>
                <div className="cards">{Array.from({ length: Math.min(n, 60) }, (_, i) => <i key={i} className={cn("card", fit && "fit")} />)}</div>
              </div>
            ))}
          </div>
        </div>
        {bp("Job board", columns.map(([name, n]) => `${name.toLowerCase()} ${n}`).join(" · "))}
      </>)}
      {node("jar", P.jar, <>
        <div className="px">
          <div className="jar"><i style={{ height: `${fill * 100}%` }} /></div>
          <span className="lbl">{money}</span>
        </div>
        {bp("Spend", cap ? `${money} of $${cap.toFixed(2)}` : money)}
      </>)}

      {agents.map((agent) => {
        const a = state.agents[agent]
        if (!a) return null
        const working = a.mode === "working" || a.mode === "thinking"
        const text = a.mode === "working" && a.queued ? `⏳ ${a.queued} in the career-lens queue` : a.text
        const role = agent === "main" ? "main agent" : agent
        return node(agent, places[agent], <>
          <div className={cn("bubble", a.mode === "idle" && "quiet")} style={{ maxWidth: agent === "main" ? 118 : bubbleWidth }}>{text}</div>
          <div className="px">
            <div className="sprite"><Sprite look={LOOKS[agent] ?? LOOKS.other} /></div>
            <div className="table"><div className="monitor" /></div>
            <span className="badge" aria-hidden>{a.mode === "done" ? "✅" : a.queued ? "⏳" : ""}</span>
            <span className="lbl">{nameOf(agent)}</span>
          </div>
          {bp(nameOf(agent), `${role}: ${text}`, working)}
        </>, { className: cn("agent", working ? "working" : a.mode === "idle" && "idle"), color: agentVar(agent) })
      })}

      {node("reader", P.reader, <>
        <div className={cn("bubble", !state.reader && "quiet")}>{state.reader ? "Reading 1 page for the agent" : "Waiting for a page"}</div>
        <div className="px">
          <div className="sprite"><Sprite look={LOOKS.reader} /></div>
          <div className="table"><div className="monitor" /></div>
          <span className="lbl">Reader<small>small model</small></span>
        </div>
        {bp("Reader", state.reader ? "reading a page" : "small model", state.reader)}
      </>, { className: state.reader ? "working" : "idle" })}
      {node("archive", P.archive, <>
        <div className="px"><div className="cabinet"><i /><i /><i /><i /></div><span className="lbl">Archive</span></div>
        {bp("Postgres", "goals · roles · steps")}
      </>)}
      {node("gate", P.gate, <>
        <div className="px"><div className={cn("door", state.gate)} /><span className="lbl">Gate</span></div>
        {bp("Gate", state.gate === "stop" ? "refused a request" : "site policy · request check", state.gate !== null)}
      </>, { className: "narrow" })}
      {node("dispatcher", P.dispatcher, <>
        <div className={cn("bubble", !state.dispatcher && "quiet")}>
          {state.tickets.length ? `${state.tickets.length} ${state.tickets.length === 1 ? "ticket" : "tickets"} in the queue` : state.dispatcher ? "Sorting the mail" : "No mail"}
        </div>
        <div className="px">
          <div className="sprite"><Sprite look={LOOKS.dispatcher} /></div>
          <div className="table"><div className="monitor" /></div>
          <span className="lbl">Dispatcher</span>
        </div>
        {bp("API + dispatcher", "/v1 · gives work to a worker", state.dispatcher)}
      </>, { className: cn("narrow", state.dispatcher ? "working" : "idle") })}
      {node("queue", P.queue, <>
        <div className="px">
          <div className="shelf">
            {state.tickets.length ? state.tickets.slice(0, 5).map((t) => (
              <div key={t.request.id} className="ticket" style={{ "--c": agentVar(t.request.agent) } as CSSProperties}>
                <b>{t.request.tool}</b> · {t.wait}
              </div>
            )) : <div className="empty">No tickets</div>}
            {state.tickets.length > 5 && <div className="ticket">+{state.tickets.length - 5} more</div>}
          </div>
          <span className="lbl">Queue · pacing</span>
        </div>
        {bp("Job queue", state.tickets.length ? `${state.tickets.length} waiting: ${state.tickets[0].wait}` : "empty", state.tickets.length > 0)}
      </>, { className: "narrow" })}
      {(shown.length ? shown : [null]).map((w, i) => node(`worker-${i}`, WORKERS[i], <>
        <div className="px">
          <div className={cn("browser", !w && "idle")}>
            <div className="bar"><i /><i /><i /></div>
            <div className="url">{w ? w.request.target || w.request.tool : "idle"}</div>
            <div className="pg" />
            <div className="prog" style={{ width: `${(w?.progress ?? 0) * 100}%` }} />
          </div>
          <span className="lbl">Chrome{i === shown.length - 1 && more > 0 ? <small>+{more} more</small> : null}</span>
        </div>
        {bp("Worker · Chrome", w ? `open: ${w.request.target}` : "idle", Boolean(w))}
      </>, { info: "worker", color: w ? agentVar(w.request.agent) : undefined }))}
      {node("history", P.history, <>
        <div className="px">
          <div className={cn("books", state.history.flash && "flash")}>
            {Array.from({ length: 14 }, (_, i) => (
              <i key={i} style={{ height: 18 + ((i * 7) % 16), background: ["#b91c1c", "#1d4ed8", "#ca8a04", "#15803d", "#7c3aed", "#0e7490"][i % 6] }} />
            ))}
          </div>
          <span className="lbl">History · {state.history.reused} reused</span>
        </div>
        {bp("History", `${state.history.reused} answered from the history`, state.history.flash)}
      </>)}
      {node("google", P.google, <>
        <div className="px"><div className={cn("bld b-google", state.sites.google && "flash-site")}>Google</div></div>
        {bp("Google", "search results", state.sites.google)}
      </>)}
      {node("linkedin", P.linkedin, <>
        <div className="px"><div className={cn("bld b-li", state.sites.linkedin && "flash-site")}><span className="sign">{liSign}</span>LinkedIn</div></div>
        {bp("LinkedIn", liSign.toLowerCase(), state.linkedin === "open")}
      </>)}
      {node("careers", P.careers, <>
        <div className="px">
          <div className={cn("bld b-careers", state.sites.careers && "flash-site")}>
            <div className="house" style={{ background: "#fca5a5" }}>lever</div>
            <div className="house" style={{ background: "#c4b5fd" }}>ashby</div>
            <div className="house" style={{ background: "#86efac" }}>green<br />house</div>
          </div>
        </div>
        {bp("Careers sites", "Lever · Ashby · Greenhouse", state.sites.careers)}
      </>)}

      {state.movers.map((m, i) => {
        const [x, y] = along(m.path.map((name) => place(name, m)), m.f)
        const seal = agentVar(m.agent)
        return (
          <div key={i}>
            <div className="fx" style={{ left: x, top: y }}>
              {m.kind === "page" ? <Page /> : m.kind === "runner" ? <span style={{ fontSize: 15 }}>🤖</span> : <Envelope seal={seal} refused={m.tone === "refused"} />}
            </div>
            {m.caption && <div className={cn("cap", m.tone)} style={{ left: x, top: y }}>{m.caption}</div>}
          </div>
        )
      })}
      {state.gate === "stop" && <div className="stamp" style={{ left: P.gate[0], top: P.gate[1] }}>REFUSED</div>}
    </div>
  )
}

