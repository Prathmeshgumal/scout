import { Check, ChevronLeft, ChevronRight, Copy, ExternalLink, Funnel, Loader2, Trash2 } from "lucide-react"
import { useEffect, useRef, useState } from "react"
import { toast } from "sonner"
import { companyFacts, noteText } from "@/components/jobs/JobPanel"
import { Monogram, VerdictBadge } from "@/components/jobs/JobRow"
import { Button } from "@/components/ui/button"
import { Select } from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { addPipelineNote, deletePipelineNote, type Evidence, type JobDetail, type PipelineNote, type PipelineStage, type Verdict } from "@/lib/api"
import { EVIDENCE_LABEL, isClosed, postedText } from "@/lib/jobs"
import { STAGE_CHOICES, canAskApplied } from "@/lib/pipeline"
import { cn } from "@/lib/utils"

const message = (err: unknown) => (err instanceof Error ? err.message : "The change failed.")

function Block({ title, action, children }: { title: string; action?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section aria-label={title} className="flex flex-col gap-2.5">
      <div className="flex min-h-7 items-center justify-between gap-3">
        <h2 className="text-[14px] font-semibold">{title}</h2>
        {action}
      </div>
      {children}
    </section>
  )
}

/** A long part of the role that stays closed until the user asks for it. */
function More({ title, children }: { title: string; children: React.ReactNode }) {
  const [open, setOpen] = useState(false)
  return (
    <section aria-label={title} className="border-t">
      <button type="button" aria-expanded={open} onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 rounded py-2.5 text-left font-medium outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-violet-500/40">
        <ChevronRight className={cn("size-4 text-muted-foreground transition-transform", open && "rotate-90")} aria-hidden />
        {title}
      </button>
      {open && <div className="pb-4 pl-6">{children}</div>}
    </section>
  )
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[84px_minmax(0,1fr)] items-start gap-2">
      <dt className="pt-px text-[12.5px] text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  )
}

/** The facts of a role that the page can show before the full details arrive. */
export type RoleFacts = {
  title: string
  company_name: string
  location: string | null
  workplace_type: string | null
  posted_at?: string | null
  posting_status: string | null
  url: string
  evidence_level?: Evidence
  fit: Verdict | null
  fit_reasons: string | null
  job_id: string | null
  goal_id: string | null
  goal_title?: string | null
}

type Props = {
  role: RoleFacts
  /** Null until the details arrive. */
  roleKey: string | null
  /** Null: the role is not in the pipeline. */
  stage: PipelineStage | null
  stageKnown: boolean
  notes: PipelineNote[]
  detail: JobDetail | null
  failed: boolean
  /** The name of the page that Back opens. */
  backLabel: string
  /** The verdict label of a role with no verdict. */
  noVerdict?: string
  draft: string
  onDraft: (text: string) => void
  onBack: () => void
  onMove: (stage: PipelineStage | "removed") => void
  onChanged: () => void
  onOpenGoal: (id: string) => void
  /** Opens the board and shows the card of this role. */
  onShowInPipeline?: () => void
}

/** The page of one role: what to read on the left, and its stage and facts on the right. */
export function RolePage({ role, roleKey, stage, stageKnown, notes, detail, failed, backLabel, noVerdict = "No verdict yet", draft, onDraft, onBack, onMove, onChanged, onOpenGoal, onShowInPipeline }: Props) {
  const [asked, setAsked] = useState(false)
  const [copied, setCopied] = useState(false)
  const [saving, setSaving] = useState(false)
  const heading = useRef<HTMLHeadingElement>(null)
  const back = useRef(onBack)
  back.current = onBack
  const { job_id: jobId, goal_id: goalId } = role

  // The parent page renders again each few seconds. These effects run once, so that they do not take the focus from the note box.
  useEffect(() => {
    heading.current?.focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented) return
      if (e.target instanceof HTMLElement && e.target.closest("input, textarea")) return
      if (document.querySelector('[role="dialog"], [role="listbox"], [role="menu"]')) return
      back.current()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [])

  const copy = async () => {
    if (!detail) return
    try {
      await navigator.clipboard.writeText(noteText(detail))
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      toast.error("The browser refused the copy. Select the text and copy it.")
    }
  }

  async function saveNote(e: React.FormEvent) {
    e.preventDefault()
    if (!draft.trim() || !roleKey) return
    setSaving(true)
    try {
      await addPipelineNote(roleKey, draft.trim())
      onDraft("")
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

  const reasons = detail?.fit_reasons ?? role.fit_reasons
  const evidence = detail?.evidence_level ?? role.evidence_level
  const facts = companyFacts(detail)
  const description = detail?.description_text || detail?.snippet
  const place = [role.company_name, role.location, role.workplace_type, postedText(role.posted_at)].filter(Boolean)

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <nav aria-label="Breadcrumb" className="flex h-11 shrink-0 items-center gap-1 border-b px-2">
        <Button variant="ghost" size="sm" onClick={onBack}><ChevronLeft aria-hidden /> {backLabel}</Button>
        <span className="text-muted-foreground" aria-hidden>/</span>
        <span className="min-w-0 truncate px-1.5 text-muted-foreground">{role.title}</span>
      </nav>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto grid max-w-[1080px] gap-x-12 gap-y-6 p-5 lg:grid-cols-[minmax(0,1fr)_280px] lg:px-8 lg:py-8">
          <header className="flex items-start gap-3.5">
            <Monogram company={role.company_name} className="size-11 rounded-xl text-[14px]" />
            <div className="min-w-0">
              <h1 ref={heading} tabIndex={-1} className="text-[22px] leading-tight font-semibold tracking-tight break-words outline-none">{role.title}</h1>
              <p className="mt-1 text-muted-foreground">{place.join(" · ")}</p>
            </div>
          </header>

          <aside aria-label="Role details" className="flex flex-col gap-4 lg:sticky lg:top-8 lg:col-start-2 lg:row-span-2 lg:row-start-1 lg:self-start">
            <div className="flex flex-col gap-2">
              {stageKnown && (
                <Select label="Stage of this role" value={stage ?? "removed"} onChange={(next) => onMove(next as PipelineStage | "removed")} className="w-full"
                  choices={stage ? STAGE_CHOICES : [{ value: "removed", label: "Not in the pipeline" }, { value: "fit", label: "Add to pipeline" }]} />
              )}
              {stage && onShowInPipeline && <Button variant="outline" onClick={onShowInPipeline}><Funnel aria-hidden /> Show in pipeline</Button>}
              {isClosed(role) ? <p className="py-1.5 text-center font-medium text-muted-foreground">This role is closed</p> : (
                <Button variant="outline" asChild>
                  <a href={role.url} target="_blank" rel="noopener noreferrer" onClick={() => setAsked(true)}>Open role <ExternalLink aria-hidden /></a>
                </Button>
              )}
              {asked && stageKnown && canAskApplied(stage) && (
                <div className="flex flex-col gap-2 rounded-lg bg-violet-500/10 p-2.5">
                  <span>Did you apply to this role?</span>
                  <span className="flex gap-1.5">
                    <Button size="sm" onClick={() => { setAsked(false); onMove("applied") }}>Mark as applied</Button>
                    <Button size="sm" variant="ghost" onClick={() => setAsked(false)}>Not yet</Button>
                  </span>
                </div>
              )}
            </div>
            <dl className="flex flex-col gap-2.5 border-t pt-4">
              <Fact label="Verdict"><VerdictBadge verdict={role.fit} stage={noVerdict} /></Fact>
              {evidence && <Fact label="Evidence">{EVIDENCE_LABEL[evidence]}</Fact>}
              <Fact label="Mission">
                {goalId
                  ? <button type="button" className="rounded text-left break-words outline-none hover:underline focus-visible:ring-2 focus-visible:ring-violet-500/40" onClick={() => onOpenGoal(goalId)}>{role.goal_title ?? "Open mission"}</button>
                  : <span className="text-muted-foreground">The mission of this role is deleted.</span>}
              </Fact>
              {detail && <Fact label="Source">{detail.source.replace(/_/g, " ")}</Fact>}
              {detail?.observed_at && <Fact label="Seen">{new Date(detail.observed_at).toLocaleDateString(undefined, { dateStyle: "medium" })}</Fact>}
            </dl>
            {stage && (
              <div className="border-t pt-2">
                <Button variant="ghost" size="sm" className="-ml-2.5 text-muted-foreground" onClick={() => onMove("removed")}>Remove from the pipeline</Button>
              </div>
            )}
          </aside>

          <div className="flex min-w-0 flex-col gap-7 lg:col-start-1">
            {failed && <p className="text-red-700 dark:text-red-300">Could not read the full details of this role. Go back and open it again.</p>}
            {jobId && !detail && !failed && <p className="flex items-center gap-2 text-muted-foreground"><Loader2 className="size-3.5 animate-spin" aria-hidden /> Reading the role</p>}
            {reasons && (
              <Block title="Why this verdict">
                <p className="max-w-[68ch] leading-relaxed break-words">{reasons}</p>
                {detail && detail.fit_evidence.length > 0 && (
                  <ul className="flex max-w-[68ch] flex-col gap-1.5">
                    {detail.fit_evidence.map((quote) => <li key={quote} className="border-l-2 border-violet-500/40 pl-3 text-muted-foreground break-words">{quote}</li>)}
                  </ul>
                )}
              </Block>
            )}
            {detail?.note && (
              <Block title="Application note" action={
                <Button variant="ghost" size="sm" onClick={() => void copy()}>{copied ? <Check aria-hidden /> : <Copy aria-hidden />} {copied ? "Copied" : "Copy note"}</Button>
              }>
                <div className="flex max-w-[68ch] flex-col gap-3.5 leading-relaxed break-words">
                  <div><h3 className="font-medium">Why this role matches</h3><p className="text-foreground/85">{detail.note.why_match}</p></div>
                  <div><h3 className="font-medium">Resume points to put first</h3><p className="whitespace-pre-line text-foreground/85">{detail.note.resume_points}</p></div>
                  <div><h3 className="font-medium">Gaps to prepare for</h3><p className="text-foreground/85">{detail.note.gaps}</p></div>
                </div>
              </Block>
            )}
            {stageKnown && <Block title="Your notes">
              {!stage && <p className="text-muted-foreground">Add this role to the pipeline to write notes.</p>}
              {notes.length > 0 && (
                <ul className="flex flex-col gap-1">
                  {notes.map((note) => (
                    <li key={note.id} className="group flex items-start gap-2 rounded-lg bg-muted/40 px-3 py-2">
                      <div className="min-w-0 flex-1">
                        <p className="break-words whitespace-pre-line">{note.text}</p>
                        <p className="mt-0.5 text-[12px] text-muted-foreground">{new Date(note.created_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}</p>
                      </div>
                      <Button variant="ghost" size="icon" className="size-7" aria-label="Delete this note" onClick={() => void removeNote(note.id)}><Trash2 /></Button>
                    </li>
                  ))}
                </ul>
              )}
              {stage && <form onSubmit={saveNote} className="flex flex-col gap-2">
                <Textarea aria-label={`Add a note to ${role.title}`} placeholder="Add a note, for example who gave a referral" maxLength={5000} rows={2}
                  value={draft} onChange={(e) => onDraft(e.target.value)} />
                {draft.trim() && <Button type="submit" size="sm" variant="outline" className="self-end" disabled={saving}>Save note</Button>}
              </form>}
            </Block>}
            {(description || facts.length > 0 || (detail?.notes.length ?? 0) > 0) && (
              <div className="border-b">
                {description && (
                  <More title={detail?.description_text ? "Job description" : "Search snippet"}>
                    <p className="max-w-[68ch] leading-relaxed break-words whitespace-pre-line">{description}</p>
                  </More>
                )}
                {facts.length > 0 && (
                  <More title="Company facts">
                    <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1.5">
                      {facts.map((fact) => (
                        <div key={fact.key} className="contents">
                          <dt className="text-muted-foreground">{fact.label}</dt>
                          <dd className="break-words">{fact.value}{fact.from ? ` (from ${fact.from})` : ""}</dd>
                        </div>
                      ))}
                    </dl>
                  </More>
                )}
                {detail && detail.notes.length > 0 && (
                  <More title="Agent notes">
                    <ul className="flex flex-col gap-1.5">
                      {detail.notes.map((note) => <li key={note.at + note.text} className="break-words"><span className="text-muted-foreground">{note.agent}:</span> {note.text}</li>)}
                    </ul>
                  </More>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
