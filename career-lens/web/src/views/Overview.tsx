import { Page, Problem, Status } from "@/components/Status"
import type { Overview as Data } from "@/lib/api"
import { ago, kindText, waitText } from "@/lib/format"

type Props = { data: Data | null; error: string | null; onOpen: (id: string) => void; onSeeAll: () => void; onWorkers: () => void }

export function Overview({ data, error, onOpen, onSeeAll, onWorkers }: Props) {
  const waiting = data?.accounts.filter((account) => account.assignment?.waiting_for_you) ?? []
  const live = data?.workers.filter((worker) => worker.live).length ?? 0
  const queued = data?.requests.filter((row) => row.status === "queued") ?? []
  const reason = queued.map((row) => waitText(row.wait_reason)).find(Boolean)
  return (
    <Page title="Overview" note="The state of the workers, the accounts and the newest requests.">
      <Problem text={error} />
      {waiting.length > 0 && (
        <section className="mb-5 rounded-xl bg-amber-500/12 px-4 py-3" aria-label="Waiting for you">
          <h2 className="text-sm font-medium text-amber-800 dark:text-amber-200">Waiting for you</h2>
          <ul className="mt-1.5 flex flex-col gap-1 text-sm">
            {waiting.map((account) => <li key={account.id}><b className="font-medium">{account.label}:</b> {account.assignment?.action}</li>)}
          </ul>
          <button type="button" onClick={onWorkers} className="mt-1.5 text-sm underline underline-offset-2">Open workers</button>
        </section>
      )}
      <dl className="mb-6 grid grid-cols-2 gap-x-6 gap-y-4 md:grid-cols-4">
        <Fact label="Live workers" value={data ? `${live} of ${data.workers.length}` : "..."} />
        <Fact label="LinkedIn accounts" value={data ? String(data.accounts.length) : "..."} />
        <Fact label="Queued, of the newest 50" value={data ? String(queued.length) : "..."} />
        <Fact label="Queue waits for" value={reason ?? "Nothing"} small />
      </dl>
      <div className="mb-2 flex items-center justify-between">
        <h2 className="text-sm font-medium">Newest requests</h2>
        <button type="button" onClick={onSeeAll} className="text-sm text-muted-foreground underline-offset-2 hover:underline">See all requests</button>
      </div>
      {data && !data.requests.length && <p className="py-8 text-center text-sm text-muted-foreground">No request yet. Use Try it to send one.</p>}
      <ul className="divide-y rounded-xl border empty:hidden">
        {data?.requests.slice(0, 8).map((row) => (
          <li key={row.id}>
            <button type="button" onClick={() => onOpen(row.id)} className="flex w-full items-center gap-3 px-3 py-2.5 text-left outline-none hover:bg-muted/60 focus-visible:bg-muted/60">
              <Status status={row.status} />
              <span className="min-w-0 flex-1 truncate text-sm">{row.target}</span>
              <span className="hidden text-xs text-muted-foreground sm:inline">{kindText(row.kind)}</span>
              <span className="text-xs whitespace-nowrap text-muted-foreground tabular-nums">{ago(row.requested_at)}</span>
            </button>
          </li>
        ))}
      </ul>
    </Page>
  )
}

function Fact({ label, value, small }: { label: string; value: string; small?: boolean }) {
  return <div className="min-w-0"><dt className="text-xs text-muted-foreground">{label}</dt><dd className={small ? "mt-1 text-sm" : "text-2xl font-semibold tabular-nums"}>{value}</dd></div>
}
