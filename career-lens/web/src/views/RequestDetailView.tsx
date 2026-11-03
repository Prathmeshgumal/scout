import { Check, ChevronLeft, ChevronRight, Copy, ExternalLink } from "lucide-react"
import { useState } from "react"
import { Problem, Status } from "@/components/Status"
import { Button } from "@/components/ui/button"
import { usePoll } from "@/hooks/usePoll"
import { type RequestDetail, type RunEvent, requestDetail, requestResult, screenshotUrl } from "@/lib/api"
import { elapsed, isFinished, kindText, waitText, when, words } from "@/lib/format"
import { cn } from "@/lib/utils"

/** "1 min 4 s" between two times. */
function took(start: string | null, end: string | null): string | null {
  if (!start || !end) return null
  const seconds = Math.max(0, Math.round((Date.parse(end) - Date.parse(start)) / 1000))
  return seconds < 60 ? `${seconds} s` : `${Math.floor(seconds / 60)} min ${seconds % 60} s`
}

export function RequestDetailView({ id, onBack }: { id: string; onBack: () => void }) {
  const [finished, setFinished] = useState(false)
  const { data, error } = usePoll(async () => {
    const value = await requestDetail(id)
    setFinished(isFinished(value.status))
    return value
  }, finished ? null : 3000, id)

  const title = data ? data.label || data.url || data.query || data.request_id : "Request"
  return (
    <div className="flex min-h-full flex-col">
      <nav aria-label="Breadcrumb" className="sticky top-0 z-10 flex h-11 shrink-0 items-center gap-1 border-b bg-background/95 px-3 backdrop-blur">
        <Button variant="ghost" size="sm" onClick={onBack}><ChevronLeft aria-hidden /> Requests</Button>
        <span className="text-muted-foreground" aria-hidden>/</span>
        <span className="min-w-0 truncate px-1.5 text-sm text-muted-foreground">{title}</span>
      </nav>
      {!data ? <div className="p-6"><Problem text={error} />{!error && <p className="text-sm text-muted-foreground">Reading the request</p>}</div> : (
        <div className="grid w-full gap-x-10 gap-y-6 px-4 py-6 sm:px-8 lg:grid-cols-[minmax(0,1fr)_320px]">
          <div className="flex min-w-0 flex-col gap-7">
            <header>
              <p className="text-xs text-muted-foreground">{kindText(data.kind)}</p>
              <h1 className="mt-1 text-xl font-semibold tracking-tight break-all">{title}</h1>
            </header>
            <Problem text={error} />
            {data.failure && (
              <section className="rounded-lg bg-red-500/10 px-3.5 py-3 text-sm">
                <h2 className="font-medium text-red-700 dark:text-red-300">{words(data.failure.kind)}</h2>
                {data.failure.detail && <p className="mt-1 break-words whitespace-pre-wrap">{data.failure.detail}</p>}
              </section>
            )}
            <RunLog data={data} />
            {data.screenshots.length > 0 && (
              <Block title="Screenshots" count={data.screenshots.length}>
                <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
                  {data.screenshots.map((shot) => (
                    <a key={shot.id} href={screenshotUrl(shot.id)} target="_blank" rel="noreferrer"
                      className="group block overflow-hidden rounded-lg border bg-muted/30 outline-none focus-visible:ring-2 focus-visible:ring-violet-500/40">
                      <img src={screenshotUrl(shot.id)} alt={`Screenshot ${shot.ordinal + 1}`} loading="lazy" className="h-64 w-full object-cover object-top transition-opacity group-hover:opacity-90" />
                    </a>
                  ))}
                </div>
              </Block>
            )}
            {data.captures.length > 0 && (
              <Block title="Captured pages" count={data.captures.length}>
                <ul className="divide-y rounded-xl border text-sm">
                  {data.captures.map((capture) => (
                    <li key={capture.id} className="flex items-center gap-3 px-3 py-2">
                      <span className="min-w-0 flex-1 truncate">{capture.section || capture.url_final || capture.id}</span>
                      {capture.http_status && <Status status={`HTTP ${capture.http_status}`} tone={capture.http_status < 400 ? "good" : "bad"} />}
                    </li>
                  ))}
                </ul>
              </Block>
            )}
            {data.extraction && <Result id={id} />}
          </div>
          <Facts data={data} />
        </div>
      )}
    </div>
  )
}

function Facts({ data }: { data: RequestDetail }) {
  const wait = data.status === "queued" ? waitText(data.wait_reason) : null
  const duration = took(data.started_at, data.finished_at)
  const link = data.url?.startsWith("http") ? data.url : null
  return (
    <aside aria-label="Request facts" className="flex flex-col gap-4 lg:sticky lg:top-16 lg:self-start">
      <div className="flex flex-wrap items-center gap-2">
        <Status status={data.status} />
        {wait && <span className="text-sm text-amber-700 dark:text-amber-300">{wait}</span>}
        {data.cancel_requested_at && !isFinished(data.status) && <span className="text-sm text-muted-foreground">Stopping</span>}
      </div>
      <dl className="flex flex-col gap-2.5 border-t pt-4 text-sm">
        <Fact label="Requested">{when(data.requested_at)}</Fact>
        <Fact label="Started">{when(data.started_at)}</Fact>
        <Fact label="Finished">{when(data.finished_at)}</Fact>
        {duration && <Fact label="Took">{duration}</Fact>}
        <Fact label="Attempts">{data.attempts.length} of {data.max_attempts}</Fact>
        {data.next_attempt_at && !isFinished(data.status) && <Fact label="Next attempt">{when(data.next_attempt_at)}</Fact>}
        {data.extraction && <Fact label="Result">{words(data.extraction.outcome)}</Fact>}
      </dl>
      <div className="flex flex-col gap-2 border-t pt-4">
        <CopyId id={data.request_id} />
        {link && (
          <Button variant="outline" size="sm" asChild>
            <a href={link} target="_blank" rel="noreferrer">Open the page <ExternalLink aria-hidden /></a>
          </Button>
        )}
      </div>
    </aside>
  )
}

function CopyId({ id }: { id: string }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(id)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch { /* The browser refused. The id stays on the screen. */ }
  }
  return (
    <div className="flex items-center gap-2">
      <span className="min-w-0 flex-1">
        <span className="block text-xs text-muted-foreground">Request id</span>
        <span className="block truncate font-mono text-xs">{id}</span>
      </span>
      <Button variant="ghost" size="icon-sm" aria-label="Copy the request id" onClick={() => void copy()}>{copied ? <Check /> : <Copy />}</Button>
    </div>
  )
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[96px_minmax(0,1fr)] gap-2">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  )
}

function Block({ title, count, children, action }: { title: string; count?: number; children: React.ReactNode; action?: React.ReactNode }) {
  return (
    <section aria-label={title}>
      <div className="mb-2 flex min-h-7 items-center justify-between gap-3">
        <h2 className="text-sm font-medium">{title}{count !== undefined && <span className="ml-1.5 font-normal text-muted-foreground tabular-nums">{count}</span>}</h2>
        {action}
      </div>
      {children}
    </section>
  )
}

function RunLog({ data }: { data: RequestDetail }) {
  if (!data.events.length) return null
  const groups = new Map<number, RunEvent[]>()
  for (const event of data.events) groups.set(event.attempt, [...(groups.get(event.attempt) ?? []), event])
  return (
    <Block title="Run log" count={data.events.length}>
      <div className="flex flex-col gap-3">
        {[...groups].map(([attempt, events]) => (
          <div key={attempt} className="overflow-hidden rounded-xl border">
            {groups.size > 1 && <h3 className="border-b bg-muted/40 px-3 py-1.5 text-xs text-muted-foreground">Attempt {attempt}</h3>}
            <ol className="max-h-[60vh] overflow-y-auto py-1 text-sm">
              {events.map((event) => (
                <li key={event.id} className={cn("grid grid-cols-[4.5rem_minmax(0,1fr)] gap-3 px-3 py-1 hover:bg-muted/40", event.failed && "bg-red-500/8")}>
                  <span className="pt-px text-right font-mono text-xs text-muted-foreground tabular-nums">{elapsed(event.elapsed_ms)}</span>
                  <span className={cn("min-w-0 break-words", event.failed && "text-red-700 dark:text-red-300")}>
                    <span className="font-medium">{event.phase}</span>{event.detail && <span className="text-muted-foreground"> {event.detail}</span>}
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
  const [open, setOpen] = useState(false)
  const [text, setText] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  async function toggle() {
    setOpen(!open)
    if (text !== null || open) return
    try {
      setText(JSON.stringify(await requestResult(id), null, 2))
    } catch (err) {
      setError(err instanceof Error ? err.message : "The result is not available.")
    }
  }
  return (
    <section aria-label="Captured result" className="overflow-hidden rounded-xl border">
      <button type="button" aria-expanded={open} onClick={() => void toggle()}
        className="flex w-full items-center gap-2 px-3 py-2.5 text-left text-sm font-medium outline-none hover:bg-muted/50 focus-visible:bg-muted/50">
        <ChevronRight className={cn("size-4 text-muted-foreground transition-transform", open && "rotate-90")} aria-hidden />
        Captured result
      </button>
      {open && (
        <div className="border-t">
          <Problem text={error} />
          {text === null && !error && <p className="px-3 py-2.5 text-sm text-muted-foreground">Reading the result</p>}
          {text !== null && <pre className="max-h-[32rem] overflow-auto p-3 font-mono text-xs leading-5">{text}</pre>}
        </div>
      )}
    </section>
  )
}
