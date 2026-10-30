import { ArrowLeft } from "lucide-react"
import { useState } from "react"
import { Page, Problem, Status } from "@/components/Status"
import { Button } from "@/components/ui/button"
import { usePoll } from "@/hooks/usePoll"
import { type RequestDetail, type RunEvent, requestDetail, requestResult, screenshotUrl } from "@/lib/api"
import { elapsed, isFinished, kindText, waitText, when, words } from "@/lib/format"
import { cn } from "@/lib/utils"

export function RequestDetailView({ id, onBack }: { id: string; onBack: () => void }) {
  const [finished, setFinished] = useState(false)
  const { data, error } = usePoll(async () => {
    const value = await requestDetail(id)
    setFinished(isFinished(value.status))
    return value
  }, finished ? null : 3000, id)
  const back = <Button variant="ghost" size="sm" onClick={onBack}><ArrowLeft />All requests</Button>

  if (!data) return <Page title="Request" actions={back}><Problem text={error} /></Page>
  const wait = data.status === "queued" ? waitText(data.wait_reason) : null
  return (
    <Page title={data.label || data.url || data.query || data.request_id} note={kindText(data.kind)} actions={back}>
      <Problem text={error} />
      <div className="mb-5 flex flex-wrap items-center gap-2">
        <Status status={data.status} />
        {wait && <span className="text-sm text-amber-700 dark:text-amber-300">{wait}</span>}
        {data.cancel_requested_at && !isFinished(data.status) && <span className="text-sm text-muted-foreground">Stopping</span>}
      </div>
      {data.failure && (
        <section className="mb-5 rounded-lg bg-red-500/10 px-3 py-2.5 text-sm">
          <h2 className="font-medium text-red-700 dark:text-red-300">{words(data.failure.kind)}</h2>
          {data.failure.detail && <p className="mt-1 break-words whitespace-pre-wrap">{data.failure.detail}</p>}
        </section>
      )}
      <dl className="mb-6 grid grid-cols-2 gap-x-6 gap-y-3 text-sm md:grid-cols-4">
        <Fact label="Requested" value={when(data.requested_at)} />
        <Fact label="Started" value={when(data.started_at)} />
        <Fact label="Finished" value={when(data.finished_at)} />
        <Fact label="Attempts" value={`${data.attempts.length} of ${data.max_attempts}`} />
        {data.next_attempt_at && !isFinished(data.status) && <Fact label="Next attempt" value={when(data.next_attempt_at)} />}
        {data.extraction && <Fact label="Result" value={words(data.extraction.outcome)} />}
        <Fact label="Request id" value={data.request_id} />
      </dl>
      <RunLog data={data} />
      {data.screenshots.length > 0 && (
        <Block title={`Screenshots (${data.screenshots.length})`}>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {data.screenshots.map((shot) => (
              <a key={shot.id} href={screenshotUrl(shot.id)} target="_blank" rel="noreferrer" className="block overflow-hidden rounded-lg border">
                <img src={screenshotUrl(shot.id)} alt={`Screenshot ${shot.ordinal + 1}`} loading="lazy" className="max-h-72 w-full object-cover object-top" />
              </a>
            ))}
          </div>
        </Block>
      )}
      {data.captures.length > 0 && (
        <Block title={`Captured pages (${data.captures.length})`}>
          <ul className="divide-y rounded-xl border text-sm">
            {data.captures.map((capture) => (
              <li key={capture.id} className="flex items-center gap-3 px-3 py-2">
                <span className="min-w-0 flex-1 truncate">{capture.section || capture.url_final || capture.id}</span>
                {capture.http_status && <span className="text-xs text-muted-foreground tabular-nums">HTTP {capture.http_status}</span>}
              </li>
            ))}
          </ul>
        </Block>
      )}
      {data.extraction && <Result id={id} />}
    </Page>
  )
}

function Fact({ label, value }: { label: string; value: string }) {
  return <div className="min-w-0"><dt className="text-xs text-muted-foreground">{label}</dt><dd className="break-words">{value}</dd></div>
}

function Block({ title, children }: { title: string; children: React.ReactNode }) {
  return <section className="mb-6"><h2 className="mb-2 text-sm font-medium">{title}</h2>{children}</section>
}

function RunLog({ data }: { data: RequestDetail }) {
  if (!data.events.length) return null
  const groups = new Map<number, RunEvent[]>()
  for (const event of data.events) groups.set(event.attempt, [...(groups.get(event.attempt) ?? []), event])
  return (
    <Block title="Run log">
      <div className="flex flex-col gap-3">
        {[...groups].map(([attempt, events]) => (
          <div key={attempt} className="overflow-hidden rounded-xl border">
            {groups.size > 1 && <h3 className="border-b bg-muted/40 px-3 py-1.5 text-xs text-muted-foreground">Attempt {attempt}</h3>}
            <ol className="max-h-96 divide-y overflow-y-auto text-sm">
              {events.map((event) => (
                <li key={event.id} className="grid grid-cols-[4.5rem_minmax(0,1fr)] gap-2 px-3 py-1.5">
                  <span className="text-xs text-muted-foreground tabular-nums">{elapsed(event.elapsed_ms)}</span>
                  <span className={cn("min-w-0 break-words", event.failed && "text-red-700 dark:text-red-300")}>
                    {event.phase}{event.detail && <span className="text-muted-foreground"> {event.detail}</span>}
                  </span>
                </li>
              ))}
            </ol>
          </div>
        ))}
      </div>
    </Block>
  )
}

function Result({ id }: { id: string }) {
  const [text, setText] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  async function show() {
    try {
      setText(JSON.stringify(await requestResult(id), null, 2))
    } catch (err) {
      setError(err instanceof Error ? err.message : "The result is not available.")
    }
  }
  return (
    <Block title="Captured result">
      <Problem text={error} />
      {text === null
        ? <Button variant="outline" size="sm" onClick={() => void show()}>Show the result</Button>
        : <pre className="max-h-[32rem] overflow-auto rounded-xl border p-3 font-mono text-xs leading-5">{text}</pre>}
    </Block>
  )
}
