import { Check, Copy, ExternalLink, Loader2, X } from "lucide-react"
import { useEffect, useRef, useState } from "react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { getJob, type JobCardData, type JobDetail } from "@/lib/api"
import { EVIDENCE_LABEL, isClosed } from "@/lib/jobs"
import { Monogram, VerdictBadge } from "./JobCard"

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-1.5">
      <h3 className="font-semibold">{title}</h3>
      {children}
    </section>
  )
}

export const noteText = (job: JobDetail) => job.note
  ? `Why this role matches\n${job.note.why_match}\n\nResume points to put first\n${job.note.resume_points}\n\nGaps to prepare for\n${job.note.gaps}`
  : ""

const labelOf = (key: string) => {
  const text = key.replace(/[_-]/g, " ")
  return text[0].toUpperCase() + text.slice(1)
}

function shown(value: unknown): string {
  if (Array.isArray(value)) return value.map(shown).join(", ")
  if (value && typeof value === "object") return JSON.stringify(value)
  return String(value)
}

export function JobPanel({ job, onClose }: { job: JobCardData; onClose: () => void }) {
  const [detail, setDetail] = useState<JobDetail | null>(null)
  const [failed, setFailed] = useState(false)
  const [copied, setCopied] = useState(false)
  const closeButton = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    let active = true
    setDetail(null)
    setFailed(false)
    getJob(job.goal_id, job.id).then((d) => { if (active) setDetail(d) }).catch(() => { if (active) setFailed(true) })
    return () => { active = false }
  }, [job.goal_id, job.id])

  useEffect(() => {
    closeButton.current?.focus()
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose() }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [onClose])

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

  const facts = (detail?.research ?? []).flatMap((row) => Object.entries(row.data)
    .filter(([, value]) => value !== null && value !== "" && !(Array.isArray(value) && !value.length))
    .map(([key, value]) => ({ key: `${row.kind}-${key}`, label: labelOf(key), value: shown(value), from: row.from_goal })))

  return (
    <aside aria-label={`Details of ${job.title}`}
      className="fixed inset-0 z-40 flex flex-col bg-background min-[900px]:static min-[900px]:z-auto min-[900px]:w-[420px] min-[900px]:shrink-0 min-[900px]:border-l">
      <header className="flex items-start gap-3 border-b p-3.5">
        <Monogram company={job.company_name} />
        <div className="min-w-0 flex-1">
          <h2 className="text-[14px] leading-snug font-semibold break-words">{job.title}</h2>
          <p className="truncate text-muted-foreground">{job.company_name}</p>
        </div>
        <Button ref={closeButton} variant="ghost" size="icon" aria-label="Close details" onClick={onClose}><X /></Button>
      </header>
      <div className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto p-3.5">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
          <VerdictBadge verdict={job.fit} stage="No verdict yet" />
          <span className="text-[12px] text-muted-foreground">{EVIDENCE_LABEL[job.evidence_level]}</span>
          {isClosed(job) ? <span className="text-[12px] font-medium">Closed</span> : (
            <a href={job.url} target="_blank" rel="noopener noreferrer" className="flex items-center gap-1 text-[12px] text-violet-700 hover:underline dark:text-violet-300">
              Open role <ExternalLink className="size-3" aria-hidden />
            </a>
          )}
        </div>
        {failed && <p className="text-red-700 dark:text-red-300">Could not read this role. Close the panel and open it again.</p>}
        {!detail && !failed && <p className="flex items-center gap-2 text-muted-foreground"><Loader2 className="size-3.5 animate-spin" aria-hidden /> Reading the role</p>}
        {detail && (
          <>
            {detail.fit_reasons && (
              <Section title="Why this verdict">
                <p className="leading-relaxed break-words">{detail.fit_reasons}</p>
                {detail.fit_evidence.length > 0 && (
                  <ul className="flex flex-col gap-1">
                    {detail.fit_evidence.map((quote) => (
                      <li key={quote} className="border-l-2 border-violet-500/40 pl-2.5 text-muted-foreground break-words">{quote}</li>
                    ))}
                  </ul>
                )}
              </Section>
            )}
            {detail.note && (
              <Section title="Application note">
                <div className="flex flex-col gap-2 rounded-lg border bg-card p-3 leading-relaxed break-words">
                  <p><b className="font-medium">Why this role matches.</b> {detail.note.why_match}</p>
                  <p className="whitespace-pre-line"><b className="font-medium">Resume points to put first.</b> {detail.note.resume_points}</p>
                  <p><b className="font-medium">Gaps to prepare for.</b> {detail.note.gaps}</p>
                  <Button variant="outline" size="sm" className="self-start" onClick={() => void copy()}>
                    {copied ? <Check aria-hidden /> : <Copy aria-hidden />} {copied ? "Copied" : "Copy note"}
                  </Button>
                </div>
              </Section>
            )}
            {facts.length > 0 && (
              <Section title="Company facts">
                <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1">
                  {facts.map((fact) => (
                    <div key={fact.key} className="contents">
                      <dt className="text-muted-foreground">{fact.label}</dt>
                      <dd className="break-words">{fact.value}{fact.from ? ` (from ${fact.from})` : ""}</dd>
                    </div>
                  ))}
                </dl>
              </Section>
            )}
            {(detail.description_text || detail.snippet) && (
              <Section title={detail.description_text ? "Job description" : "Search snippet"}>
                <p className="leading-relaxed break-words whitespace-pre-line">{detail.description_text || detail.snippet}</p>
              </Section>
            )}
            {detail.notes.length > 0 && (
              <Section title="Agent notes">
                <ul className="flex flex-col gap-1">
                  {detail.notes.map((note) => <li key={note.at + note.text} className="break-words"><span className="text-muted-foreground">{note.agent}:</span> {note.text}</li>)}
                </ul>
              </Section>
            )}
            <p className="text-[12px] break-words text-muted-foreground">
              Source: {detail.source.replace(/_/g, " ")}{detail.observed_at ? `, seen ${new Date(detail.observed_at).toLocaleString()}` : ""}
            </p>
          </>
        )}
      </div>
    </aside>
  )
}
