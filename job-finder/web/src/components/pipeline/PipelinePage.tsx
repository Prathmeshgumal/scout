import { ChevronRight, ExternalLink, Trash2 } from "lucide-react"
import { useCallback, useEffect, useMemo, useState } from "react"
import { toast } from "sonner"
import { JobPanel } from "@/components/jobs/JobPanel"
import { Monogram, SkeletonRows, VerdictBadge } from "@/components/jobs/JobRow"
import { Button } from "@/components/ui/button"
import { Select } from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import {
  addPipelineNote, deletePipelineNote, getPipeline, setPipelineStage,
  type JobCardData, type PipelineItem, type PipelineStage,
} from "@/lib/api"
import { isClosed } from "@/lib/jobs"
import { STAGES, STAGE_CHOICES, canAskApplied, funnelOf } from "@/lib/pipeline"
import { cn } from "@/lib/utils"
import { Funnel } from "./Funnel"

const message = (err: unknown) => (err instanceof Error ? err.message : "The change failed.")

const asJob = (item: PipelineItem): JobCardData => ({
  id: item.job_id ?? "", goal_id: item.goal_id ?? "", goal_title: item.goal_title ?? undefined, title: item.title,
  company_name: item.company_name, location: item.location, workplace_type: item.workplace_type, posted_at: null,
  posting_status: item.posting_status, url: item.url, source: "", evidence_level: item.evidence_level ?? "listing_only",
  observed_at: null, fit: item.fit, fit_reasons: item.fit_reasons, created_at: "",
})

type RowUi = { open: boolean; asked: boolean; draft: string }
const NEW_ROW: RowUi = { open: false, asked: false, draft: "" }

type Props = { onOpenGoal: (id: string) => void; onJobs: () => void }

/** The roles that the user tracks, stage by stage, with the funnel on the right. */
export function PipelinePage({ onOpenGoal, onJobs }: Props) {
  const [items, setItems] = useState<PipelineItem[] | null>(null)
  const [failed, setFailed] = useState(false)
  const [details, setDetails] = useState<JobCardData | null>(null)
  const closeDetails = useCallback(() => setDetails(null), [])
  // A role that changes stage moves to a new list, and its row starts again. This state stays with the role.
  const [rowUi, setRowUi] = useState<Record<string, RowUi>>({})
  const changeRow = useCallback((roleKey: string, change: Partial<RowUi>) => {
    setRowUi((now) => ({ ...now, [roleKey]: { ...(now[roleKey] ?? NEW_ROW), ...change } }))
  }, [])

  const read = useCallback(() => getPipeline().then((body) => { setItems(body.items); setFailed(false) }).catch(() => setFailed(true)), [])
  useEffect(() => {
    void read()
    const timer = setInterval(() => void read(), 10_000)
    return () => clearInterval(timer)
  }, [read])

  async function move(item: PipelineItem, stage: PipelineStage | "removed") {
    setItems((now) => (now ?? []).flatMap((row) => row.role_key !== item.role_key ? [row] : stage === "removed" ? [] : [{ ...row, stage }]))
    try {
      await setPipelineStage(item.role_key, stage)
      if (stage === "removed") {
        toast.success(`Removed ${item.title} from the pipeline.`, {
          action: { label: "Undo", onClick: () => void move(item, item.stage) },
        })
      }
    } catch (err) {
      toast.error(message(err))
    }
    await read()
  }

  const steps = useMemo(() => funnelOf(items ?? []), [items])
  return (
    <div className="flex min-h-0 flex-1">
      <div className="min-w-0 flex-1 overflow-y-auto">
        <div className="mx-auto flex max-w-[1240px] flex-col gap-4 p-4">
          <header>
            <h1 className="text-[17px] font-semibold">Job pipeline</h1>
            <p className="text-muted-foreground">Fit roles start here. Move each role when your application changes.</p>
          </header>
          {failed && !items && <p className="text-red-700 dark:text-red-300">Could not read the pipeline. The page tries again in 10 seconds.</p>}
          {!items && !failed && <SkeletonRows count={4} />}
          {items?.length === 0 && (
            <div className="flex flex-col items-start gap-2 py-6">
              <p className="font-medium">The pipeline has no roles.</p>
              <p className="text-muted-foreground">A role comes here when an agent marks it as a fit, or when you choose a stage in its details.</p>
              <Button size="sm" variant="outline" onClick={onJobs}>Open Jobs</Button>
            </div>
          )}
          {items && items.length > 0 && (
            <div className="flex flex-col-reverse gap-4 lg:grid lg:grid-cols-[minmax(0,1fr)_300px] lg:items-start">
              <div className="flex min-w-0 flex-col gap-5">
                {STAGES.map((stage) => {
                  const rows = items.filter((item) => item.stage === stage.id)
                  return (
                    <section key={stage.id} aria-label={stage.label}>
                      <h2 className="mb-1.5 flex items-baseline gap-2 font-semibold">{stage.label} <span className="font-normal text-muted-foreground tabular-nums">{rows.length}</span></h2>
                      {rows.length === 0 ? <p className="rounded-xl border border-dashed px-3 py-3 text-muted-foreground">No roles in this stage.</p> : (
                        <ul className="divide-y rounded-xl border bg-card">
                          {rows.map((item) => (
                            <Row key={item.role_key} item={item} ui={rowUi[item.role_key] ?? NEW_ROW} onUi={(change) => changeRow(item.role_key, change)} onMove={(next) => void move(item, next)} onChanged={() => void read()}
                              onDetails={() => setDetails(asJob(item))} onOpenGoal={onOpenGoal} />
                          ))}
                        </ul>
                      )}
                    </section>
                  )
                })}
              </div>
              <div className="lg:sticky lg:top-4"><Funnel steps={steps} /></div>
            </div>
          )}
        </div>
      </div>
      {details && <JobPanel key={details.id} job={details} onClose={closeDetails} onStage={() => void read()} />}
    </div>
  )
}

type RowProps = {
  item: PipelineItem
  ui: RowUi
  onUi: (change: Partial<RowUi>) => void
  onMove: (stage: PipelineStage | "removed") => void
  onChanged: () => void
  onDetails: () => void
  onOpenGoal: (id: string) => void
}

function Row({ item, ui, onUi, onMove, onChanged, onDetails, onOpenGoal }: RowProps) {
  const { open, asked, draft } = ui
  const [saving, setSaving] = useState(false)
  const facts = [item.company_name, item.location, item.workplace_type].filter(Boolean)
  const noteCount = item.notes.length

  async function saveNote(e: React.FormEvent) {
    e.preventDefault()
    if (!draft.trim()) return
    setSaving(true)
    try {
      await addPipelineNote(item.role_key, draft.trim())
      onUi({ draft: "" })
      onChanged()
    } catch (err) {
      toast.error(message(err))
    }
    setSaving(false)
  }

  async function removeNote(id: string) {
    try {
      await deletePipelineNote(id)
      onChanged()
    } catch (err) {
      toast.error(message(err))
    }
  }

  return (
    <li>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-3 py-2.5">
        <button type="button" aria-expanded={open} onClick={() => onUi({ open: !open })}
          className="flex min-w-0 flex-1 basis-64 items-center gap-3 rounded-lg text-left outline-none focus-visible:ring-2 focus-visible:ring-violet-500/50">
          <ChevronRight className={cn("size-4 shrink-0 text-muted-foreground transition-transform", open && "rotate-90")} aria-hidden />
          <Monogram company={item.company_name} />
          <span className="min-w-0">
            <span className="block truncate text-[14px] font-medium">{item.title}</span>
            <span className="block truncate text-[12.5px] text-muted-foreground">
              {facts.join(" · ")}{noteCount > 0 ? ` · ${noteCount} ${noteCount === 1 ? "note" : "notes"}` : ""}
            </span>
          </span>
        </button>
        <div className="flex items-center gap-3 pl-7 sm:pl-0">
          <Select label={`Stage of ${item.title}`} value={item.stage} onChange={(stage) => onMove(stage as PipelineStage)} choices={STAGE_CHOICES} className="h-8 w-[172px] text-[13px]" />
          {isClosed(item) ? <span className="w-[74px] text-right text-[12px] font-medium text-muted-foreground">Closed</span> : (
            <a href={item.url} target="_blank" rel="noopener noreferrer" onClick={() => onUi({ asked: true })}
              className="flex w-[74px] items-center justify-end gap-1 rounded text-[12px] text-violet-700 hover:underline dark:text-violet-300">
              Open role <ExternalLink className="size-3" aria-hidden />
            </a>
          )}
        </div>
      </div>
      {asked && canAskApplied(item.stage) && (
        <div className="mx-3 mb-2.5 flex flex-wrap items-center gap-2 rounded-lg bg-violet-500/10 px-2.5 py-2">
          <span className="mr-auto">Did you apply to this role?</span>
          <Button size="sm" onClick={() => { onUi({ asked: false }); onMove("applied") }}>Mark as applied</Button>
          <Button size="sm" variant="ghost" onClick={() => onUi({ asked: false })}>Not yet</Button>
        </div>
      )}
      {open && (
        <div className="flex flex-col gap-3 border-t bg-muted/20 px-3 py-3">
          {item.fit && (
            <div className="flex items-start gap-2">
              <VerdictBadge verdict={item.fit} />
              {item.fit_reasons && <p className="min-w-0 leading-relaxed break-words">{item.fit_reasons}</p>}
            </div>
          )}
          <section aria-label="Your notes" className="flex flex-col gap-2">
            <h3 className="font-medium">Your notes</h3>
            {noteCount > 0 && (
              <ul className="flex flex-col gap-1.5">
                {item.notes.map((note) => (
                  <li key={note.id} className="flex items-start gap-2 rounded-lg border bg-card px-2.5 py-1.5">
                    <div className="min-w-0 flex-1">
                      <p className="break-words whitespace-pre-line">{note.text}</p>
                      <p className="text-[12px] text-muted-foreground">{new Date(note.created_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}</p>
                    </div>
                    <Button variant="ghost" size="icon" className="size-7" aria-label="Delete this note" onClick={() => void removeNote(note.id)}><Trash2 /></Button>
                  </li>
                ))}
              </ul>
            )}
            <form onSubmit={saveNote} className="flex flex-col gap-2">
              <Textarea aria-label={`Add a note to ${item.title}`} placeholder="Add a note, for example who gave a referral" maxLength={5000} rows={2}
                value={draft} onChange={(e) => onUi({ draft: e.target.value })} />
              <Button type="submit" size="sm" variant="outline" className="self-start" disabled={!draft.trim() || saving}>Save note</Button>
            </form>
          </section>
          <div className="flex flex-wrap items-center gap-1.5">
            {item.job_id && <Button variant="ghost" size="sm" onClick={onDetails}>Role details</Button>}
            {item.goal_id
              ? <Button variant="ghost" size="sm" onClick={() => onOpenGoal(item.goal_id!)}>Open goal</Button>
              : <span className="px-2 text-[12px] text-muted-foreground">The goal of this role is deleted.</span>}
            <Button variant="ghost" size="sm" className="ml-auto" onClick={() => onMove("removed")}>Remove from the pipeline</Button>
          </div>
        </div>
      )}
    </li>
  )
}
