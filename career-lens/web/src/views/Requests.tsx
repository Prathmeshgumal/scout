import { useEffect, useState } from "react"
import { Page, Problem, Status, fieldClass } from "@/components/Status"
import { Button } from "@/components/ui/button"
import { Select } from "@/components/ui/select"
import { usePoll } from "@/hooks/usePoll"
import { type RequestPage, type RequestRow, requests } from "@/lib/api"
import { KINDS, STATUSES, ago, kindText, waitText, words } from "@/lib/format"
import { cn } from "@/lib/utils"

export function Requests({ onOpen }: { onOpen: (id: string) => void }) {
  const [status, setStatus] = useState("")
  const [kind, setKind] = useState("")
  const [typed, setTyped] = useState("")
  const [q, setQ] = useState("")
  const [older, setOlder] = useState<RequestPage[]>([])
  const key = `${status}|${kind}|${q}`
  const { data, error } = usePoll(() => requests({ status, kind, q }), 5000, key)

  useEffect(() => {
    const timer = setTimeout(() => setQ(typed.trim()), 250)
    return () => clearTimeout(timer)
  }, [typed])
  useEffect(() => setOlder([]), [key])

  const next = older.length ? older[older.length - 1].next : data?.next
  async function loadOlder() {
    if (next) {
      const page = await requests({ status, kind, q, before: next })
      setOlder((pages) => [...pages, page])
    }
  }
  const counts = data?.counts ?? {}
  const total = Object.values(counts).reduce((sum, n) => sum + n, 0)
  const rows = [...(data?.items ?? []), ...older.flatMap((page) => page.items)]
  const seen = new Set<string>()
  const unique = rows.filter((row) => !seen.has(row.id) && seen.add(row.id))

  return (
    <Page title="Requests" note="Each scrape and search that reached career-lens.">
      <Problem text={error} />
      <div className="mb-3 flex flex-wrap gap-1" role="group" aria-label="Status">
        {["", ...STATUSES].map((id) => {
          const count = id ? counts[id] ?? 0 : total
          return (
            <button key={id || "all"} type="button" aria-pressed={status === id} onClick={() => setStatus(id)}
              className={cn("h-7 rounded-md px-2 text-xs transition-colors", status === id ? "bg-violet-500/15 text-violet-700 dark:text-violet-300" : "text-muted-foreground hover:bg-muted")}>
              {id ? words(id) : "All"} <span className="tabular-nums opacity-70">{count}</span>
            </button>
          )
        })}
      </div>
      <div className="mb-4 flex flex-col gap-2 sm:flex-row">
        <input aria-label="Search" placeholder="Search by URL or query" value={typed} onChange={(e) => setTyped(e.target.value)} className={fieldClass} />
        <Select label="Kind" value={kind || "all"} onChange={(value) => setKind(value === "all" ? "" : value)} className="w-full shrink-0 sm:w-52"
          choices={[{ value: "all", label: "All kinds" }, ...Object.entries(KINDS).map(([value, label]) => ({ value, label }))]} />
      </div>
      {data && unique.length === 0 && <p className="py-10 text-center text-sm text-muted-foreground">No request matches these filters.</p>}
      <ul className="divide-y rounded-xl border empty:hidden">
        {unique.map((row) => <Row key={row.id} row={row} onOpen={onOpen} />)}
      </ul>
      {next && <Button variant="ghost" className="mt-3 w-full" onClick={() => void loadOlder()}>Show older requests</Button>}
    </Page>
  )
}

function Row({ row, onOpen }: { row: RequestRow; onOpen: (id: string) => void }) {
  const wait = row.status === "queued" ? waitText(row.wait_reason) : null
  return (
    <li>
      <button type="button" onClick={() => onOpen(row.id)}
        className="grid w-full grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-x-3 px-3 py-2.5 text-left outline-none transition-colors hover:bg-muted/60 focus-visible:bg-muted/60">
        <Status status={row.status} />
        <span className="min-w-0">
          <span className="block truncate text-sm">{row.label || row.target || row.id}</span>
          <span className="block truncate text-xs text-muted-foreground">
            {kindText(row.kind)}{row.attempts > 1 ? `, ${row.attempts} attempts` : ""}
            {wait && <span className="text-amber-700 dark:text-amber-300">, {wait}</span>}
          </span>
        </span>
        <span className="text-xs whitespace-nowrap text-muted-foreground tabular-nums">{ago(row.requested_at)}</span>
      </button>
    </li>
  )
}
