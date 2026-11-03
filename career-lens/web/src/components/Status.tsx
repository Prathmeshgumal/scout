import { statusTone, words } from "@/lib/format"
import { cn } from "@/lib/utils"

const TONES = {
  good: "bg-emerald-500/12 text-emerald-700 dark:text-emerald-300",
  bad: "bg-red-500/12 text-red-700 dark:text-red-300",
  busy: "bg-violet-500/14 text-violet-700 dark:text-violet-300",
  wait: "bg-amber-500/14 text-amber-700 dark:text-amber-300",
  idle: "bg-muted text-muted-foreground",
}

export function Status({ status, tone }: { status: string; tone?: keyof typeof TONES }) {
  return <span className={cn("inline-flex h-5 shrink-0 items-center rounded-md px-1.5 text-xs font-medium", TONES[tone ?? statusTone(status)])}>{words(status)}</span>
}

export function Page({ title, note, children, actions }: { title: string; note?: string; children: React.ReactNode; actions?: React.ReactNode }) {
  return (
    <div className="mx-auto w-full max-w-5xl px-4 py-6 sm:px-7">
      <header className="mb-5 flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
          {note && <p className="mt-1 text-sm text-muted-foreground">{note}</p>}
        </div>
        {actions}
      </header>
      {children}
    </div>
  )
}

export function Problem({ text }: { text: string | null }) {
  if (!text) return null
  return <p role="alert" className="mb-4 rounded-lg bg-red-500/10 px-3 py-2 text-sm text-red-700 dark:text-red-300">{text}</p>
}

export const fieldClass = "h-9 w-full rounded-lg border bg-transparent px-2.5 text-sm outline-none focus-visible:border-violet-500/60 focus-visible:ring-2 focus-visible:ring-violet-500/20"

export function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <label className="flex min-w-0 flex-col gap-1 text-xs text-muted-foreground">{label}{children}</label>
}

/** A box that holds one thing, such as a worker or an account. Its rows have lines between them. */
export function Panel({ label, children, className }: { label: string; children: React.ReactNode; className?: string }) {
  return <section aria-label={label} className={cn("divide-y overflow-hidden rounded-xl border bg-card", className)}>{children}</section>
}

/** A row of a panel: the name and a short note on the left, the controls on the right. */
export function SettingRow({ title, note, children }: { title: string; note?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="grid gap-x-6 gap-y-3 px-4 py-3.5 md:grid-cols-[200px_minmax(0,1fr)] md:items-center">
      <div className="min-w-0">
        <h3 className="text-sm font-medium">{title}</h3>
        {note && <p className="mt-0.5 text-xs text-muted-foreground">{note}</p>}
      </div>
      <div className="min-w-0">{children}</div>
    </div>
  )
}
