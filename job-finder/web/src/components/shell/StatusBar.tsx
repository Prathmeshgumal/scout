import { Kbd } from "@/components/Kbd"
import type { CareerLensState, Status } from "@/lib/api"
import { cn } from "@/lib/utils"

const LENS: Record<CareerLensState, string> = {
  checking: "Checking career-lens", online: "career-lens online", unreachable: "career-lens down", error: "career-lens error",
  access_denied: "career-lens refused the key", not_configured: "career-lens not set up",
}

type Props = { status: Status | null; wait: string | null; spend: { used: number; max: number } | null; running: number; onCommand: () => void }

export function StatusBar({ status, wait, spend, running, onCommand }: Props) {
  const lens = status?.career_lens?.state ?? (status ? "checking" : "unreachable")
  const ok = lens === "online"
  return (
    <footer className="flex h-7 shrink-0 items-center gap-4 overflow-hidden border-t px-3.5 text-[11.5px] whitespace-nowrap text-muted-foreground">
      <span className="flex items-center gap-1.5">
        <i className={cn("size-1.5 rounded-full", ok ? "bg-violet-500 shadow-[0_0_8px] shadow-violet-500" : lens === "checking" ? "bg-muted-foreground" : "bg-red-500")} aria-hidden />
        {status ? LENS[lens] : "Server down"}
      </span>
      {wait && <span className="min-w-0 truncate text-amber-700 dark:text-amber-300">{wait}</span>}
      <span className="flex-1" />
      {spend && <span className="tabular-nums">${spend.used.toFixed(2)} of ${spend.max.toFixed(2)}</span>}
      {running > 0 && <span className="tabular-nums">{running} running</span>}
      <button type="button" onClick={onCommand} aria-label="Open the command menu" className="hidden rounded outline-none focus-visible:ring-2 focus-visible:ring-ring sm:block"><Kbd>Ctrl K</Kbd></button>
    </footer>
  )
}
