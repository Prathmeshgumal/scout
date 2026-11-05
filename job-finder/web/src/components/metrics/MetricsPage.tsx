import { RefreshCw } from "lucide-react"
import { useCallback, useEffect, useState, type ReactNode } from "react"
import { Button } from "@/components/ui/button"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { getMetrics, type EvalRunRow, type Metrics } from "@/lib/api"
import { cn } from "@/lib/utils"

export const PERIODS = [7, 30, 90] as const
export const SUITE_LABELS: Record<string, string> = {
  "request-check": "Request check", injection: "Prompt injection", "fit-check": "Fit check", notes: "Application notes",
}
const VERDICTS = ["fit", "partial", "not_fit", "unsure", "none"] as const

export function money(usd: number | null | undefined): string {
  if (usd === null || usd === undefined) return "–"
  return usd < 1 ? `$${usd.toFixed(3)}` : `$${usd.toFixed(2)}`
}

export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "–"
  if (seconds < 60) return `${Math.round(seconds)} s`
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`
  const hours = Math.floor(seconds / 3600)
  return `${hours} h ${Math.round((seconds % 3600) / 60)} min`
}

const errorText = (err: unknown) => (err instanceof Error ? err.message : String(err))
const percent = (share: number) => `${Math.round(share * 100)}%`

/** Each day of the period, oldest first, with 0 for a day that has no goals. */
export function fillDays(daily: Metrics["daily"], days: number, today = new Date()): Metrics["daily"] {
  const byDay = new Map(daily.map((row) => [row.day, row]))
  const out: Metrics["daily"] = []
  for (let i = days - 1; i >= 0; i--) {
    const date = new Date(today)
    date.setDate(date.getDate() - i)
    const day = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`
    out.push(byDay.get(day) ?? { day, goals: 0, spend_usd: 0, fit_roles: 0 })
  }
  return out
}

const dayLabel = (day: string) => new Date(`${day}T00:00:00`).toLocaleDateString(undefined, { day: "numeric", month: "short" })

function Tile({ label, value, sub }: { label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5 rounded-lg border bg-card px-3.5 py-3">
      <span className="text-sm text-muted-foreground">{label}</span>
      <span className="text-[22px] leading-tight font-semibold tabular-nums">{value}</span>
      {sub && <span className="text-xs text-muted-foreground tabular-nums">{sub}</span>}
    </div>
  )
}

function Section({ title, sub, children, className }: { title: string; sub?: string; children: ReactNode; className?: string }) {
  return (
    <section aria-label={title} className={cn("flex min-w-0 flex-col gap-3 rounded-lg border bg-card p-4", className)}>
      <div>
        <h2 className="text-base font-semibold">{title}</h2>
        {sub && <p className="text-sm text-muted-foreground">{sub}</p>}
      </div>
      {children}
    </section>
  )
}

/** One measure for each day, as bars. The value of a bar shows on hover and to screen readers. */
function DayBars({ title, rows, value, format }: { title: string; rows: Metrics["daily"]; value: (row: Metrics["daily"][number]) => number; format: (n: number) => string }) {
  const top = Math.max(0, ...rows.map(value))
  return (
    <Section title={title} sub={top ? `Highest day: ${format(top)}` : "No data in this period."}>
      <div role="list" aria-label={title} className="flex h-28 items-end gap-[2px] border-b">
        {rows.map((row) => {
          const n = value(row)
          return (
            <Tooltip key={row.day}>
              <TooltipTrigger asChild>
                <div role="listitem" aria-label={`${dayLabel(row.day)}: ${format(n)}`} tabIndex={0}
                  className="group flex h-full min-w-0 flex-1 cursor-default items-end outline-none focus-visible:ring-2 focus-visible:ring-ring">
                  <div className="w-full rounded-t-[3px] bg-violet-500 group-hover:bg-violet-600 dark:bg-violet-400 dark:group-hover:bg-violet-300"
                    style={{ height: top ? `${Math.max(n ? 3 : 0, (n / top) * 100)}%` : 0 }} />
                </div>
              </TooltipTrigger>
              <TooltipContent>{dayLabel(row.day)}: {format(n)}</TooltipContent>
            </Tooltip>
          )
        })}
      </div>
      {rows.length > 0 && (
        <div className="flex justify-between text-xs text-muted-foreground">
          <span>{dayLabel(rows[0].day)}</span>
          <span>{dayLabel(rows[rows.length - 1].day)}</span>
        </div>
      )}
    </Section>
  )
}

function Table({ head, rows, empty }: { head: string[]; rows: ReactNode[][]; empty: string }) {
  if (!rows.length) return <p className="text-sm text-muted-foreground">{empty}</p>
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b text-left text-muted-foreground">
            {head.map((h, i) => <th key={h} className={cn("py-1.5 pr-3 font-normal", i > 0 && "text-right")}>{h}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, r) => (
            <tr key={r} className="border-b last:border-0">
              {row.map((cell, i) => <td key={i} className={cn("py-1.5 pr-3", i > 0 && "text-right tabular-nums")}>{cell}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** A share as a short bar and a number: the score of an eval, or the failure rate of a tool. */
function Share({ share, tone = "violet" }: { share: number; tone?: "violet" | "red" }) {
  return (
    <span className="inline-flex items-center justify-end gap-2">
      <span className="h-1.5 w-16 overflow-hidden rounded-full bg-muted" aria-hidden>
        <span className={cn("block h-full rounded-full", tone === "red" ? "bg-red-500" : "bg-violet-500 dark:bg-violet-400")} style={{ width: percent(share) }} />
      </span>
      {percent(share)}
    </span>
  )
}

function Evals({ latest, runs }: { latest: EvalRunRow[]; runs: EvalRunRow[] }) {
  if (!latest.length) {
    return (
      <Section title="Evals" sub="Scores of the agents on fixed, made-up cases.">
        <p className="text-sm text-muted-foreground">No eval runs yet. Run <code className="rounded bg-muted px-1">job-finder eval</code> to score the agents.</p>
      </Section>
    )
  }
  const history = (suite: string) => runs.filter((run) => run.suite === suite).slice(0, 8).reverse()
  return (
    <Section title="Evals" sub="Scores of the agents on fixed, made-up cases. The prompt column changes when a prompt changes.">
      <Table head={["Suite", "Score", "Passed", "Last 8 runs", "Cost", "Model", "Prompt", "Run"]} empty=""
        rows={latest.map((run) => [
          SUITE_LABELS[run.suite] ?? run.suite,
          <Share key="s" share={run.score} />,
          `${run.passed}/${run.total}`,
          <span key="h" className="inline-flex items-end gap-[2px]" aria-label={`Scores: ${history(run.suite).map((r) => percent(r.score)).join(", ")}`}>
            {history(run.suite).map((r) => <span key={r.id} title={`${percent(r.score)} on ${new Date(r.created_at).toLocaleString()}`} className="w-1.5 rounded-t-[2px] bg-violet-500 dark:bg-violet-400" style={{ height: `${Math.max(2, r.score * 20)}px` }} />)}
          </span>,
          money(run.cost_usd),
          <span key="m" className="text-muted-foreground">{run.model.replace(/^claude-/, "")}</span>,
          <code key="p" className="text-xs text-muted-foreground">{run.prompt_sha.slice(0, 7)}</code>,
          <span key="d" className="text-muted-foreground">{new Date(run.created_at).toLocaleDateString(undefined, { day: "numeric", month: "short" })}</span>,
        ])} />
    </Section>
  )
}

export function MetricsPage() {
  const [days, setDays] = useState<number>(30)
  const [data, setData] = useState<Metrics | null>(null)
  const [failed, setFailed] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const read = useCallback(async (period: number) => {
    setBusy(true)
    try {
      setData(await getMetrics(period))
      setFailed(null)
    } catch (err) {
      setFailed(errorText(err))
    } finally {
      setBusy(false)
    }
  }, [])

  useEffect(() => { void read(days) }, [days, read])

  const t = data?.totals
  const rows = data ? fillDays(data.daily, data.days) : []
  const models = [...new Set(data?.verdicts.map((v) => v.model) ?? [])]
  const verdictCount = (model: string, verdict: string) => data?.verdicts.find((v) => v.model === model && v.verdict === verdict)?.roles ?? 0

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
      <div className="mx-auto flex w-full max-w-[1100px] flex-col gap-4 px-4 py-5">
        <header className="flex flex-wrap items-center gap-3">
          <div className="mr-auto">
            <h1 className="text-[22px] leading-tight font-semibold">Metrics</h1>
            <p className="text-sm text-muted-foreground">Cost, time, results and quality of the goals that you started.</p>
          </div>
          <div role="group" aria-label="Period" className="inline-flex rounded-md border p-0.5">
            {PERIODS.map((p) => (
              <button key={p} type="button" aria-pressed={days === p} onClick={() => setDays(p)}
                className={cn("rounded-[5px] px-2.5 py-1 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring", days === p ? "bg-muted font-medium" : "text-muted-foreground hover:text-foreground")}>
                {p} days
              </button>
            ))}
          </div>
          <Button variant="ghost" size="icon" aria-label="Refresh" onClick={() => void read(days)} disabled={busy}>
            <RefreshCw className={cn(busy && "animate-spin")} />
          </Button>
        </header>

        {failed && <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm">{failed}</p>}
        {!data && !failed && <p className="text-muted-foreground">Reading the metrics</p>}

        {data && t && (
          <>
            <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
              <Tile label="Spend" value={money(t.spend_usd)} sub={`${t.goals} goals, ${t.mandate_goals} from mandates`} />
              <Tile label="Fit roles" value={t.fit_roles} sub={`of ${t.roles} roles found`} />
              <Tile label="Cost per fit role" value={money(t.cost_per_fit_usd)} />
              <Tile label="Time per goal" value={duration(t.median_busy_s)} sub={`median; 90% under ${duration(t.p90_busy_s)}`} />
              <Tile label="Goals" value={`${t.done} done`} sub={`${t.stopped} stopped, ${t.failed} failed, ${t.running} running`} />
              <Tile label="Refused pages" value={data.refused.pages} sub={`${data.refused.search_results} search results removed`} />
            </div>

            <div className="grid gap-3 md:grid-cols-2">
              <DayBars title="Spend each day" rows={rows} value={(r) => r.spend_usd} format={money} />
              <DayBars title="Fit roles each day" rows={rows} value={(r) => r.fit_roles} format={(n) => `${n} fit role${n === 1 ? "" : "s"}`} />
            </div>

            <Evals latest={data.evals.latest} runs={data.evals.runs} />

            <div className="grid gap-3 lg:grid-cols-2">
              <Section title="Models" sub="Spend and verdicts by the model of the goal.">
                <Table head={["Model", "Goals", "Spend", "Fit", "Partial", "Not fit", "Unsure", "Cost per fit"]} empty="No goals in this period."
                  rows={models.map((model) => {
                    const m = data.by_model.find((row) => row.model === model)
                    return [model.replace(/^claude-/, ""), m?.goals ?? 0, money(m?.spend_usd ?? 0), ...VERDICTS.slice(0, 4).map((v) => verdictCount(model, v)),
                      money(m && m.fit_roles ? m.spend_usd / m.fit_roles : null)]
                  })} />
              </Section>
              <Section title="Agents" sub="Subagent runs and their median time.">
                <Table head={["Agent", "Runs", "Failed", "Median time"]} empty="No subagent runs in this period."
                  rows={data.agents.map((a) => [a.agent, a.runs, a.failed, duration(a.median_s)])} />
              </Section>
              <Section title="Scrapes" sub="Requests to career-lens. Wait: from the request to the result.">
                <Table head={["Kind", "Total", "Done", "Failed", "Cancelled", "Median wait", "90% wait"]} empty="No scrapes in this period."
                  rows={data.scrapes.map((s) => [s.kind.replaceAll("_", " "), s.total, s.succeeded, s.failed, s.cancelled, duration(s.median_wait_s), duration(s.p90_wait_s)])} />
              </Section>
              <Section title="Tools" sub="Tool calls of the agents, with the most failures first.">
                <Table head={["Tool", "Calls", "Failed", "Failure rate"]} empty="No tool calls in this period."
                  rows={data.tools.map((tool) => [tool.tool, tool.calls, tool.failed, <Share key="f" share={tool.calls ? tool.failed / tool.calls : 0} tone="red" />])} />
              </Section>
              <Section title="Failure kinds" sub="Why scrapes and tool calls failed." className="lg:col-span-2">
                <Table head={["Kind", "Source", "Count"]} empty="No failures in this period."
                  rows={data.failure_kinds.map((f) => [f.kind.replaceAll("_", " "), f.source === "scrape" ? "Scrape" : "Tool call", f.count])} />
              </Section>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
