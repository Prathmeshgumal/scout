import type { FunnelStep } from "@/lib/pipeline"
import { cn } from "@/lib/utils"

const BAND = 28
// One hue that deepens stage by stage.
const FILLS = [
  "fill-violet-300 dark:fill-violet-800",
  "fill-violet-400 dark:fill-violet-700",
  "fill-violet-500 dark:fill-violet-500",
  "fill-violet-700 dark:fill-violet-300",
]

/** A ring that fills stage by stage, so each column can be told apart at a glance. */
export function StageIcon({ index, total }: { index: number; total: number }) {
  const length = 2 * Math.PI * 2.5
  const share = index / (total - 1)
  return (
    <svg viewBox="0 0 14 14" className="size-3.5 shrink-0 text-violet-600 dark:text-violet-400" aria-hidden>
      <circle cx="7" cy="7" r="6" fill="none" stroke="currentColor" strokeWidth="1.5" strokeDasharray={index === 0 ? "2 2" : undefined} />
      {share > 0 && <circle cx="7" cy="7" r="2.5" fill="none" stroke="currentColor" strokeWidth="5" strokeDasharray={`${share * length} ${length}`} transform="rotate(-90 7 7)" />}
    </svg>
  )
}

type Props = { steps: FunnelStep[]; counts: Record<string, number>; className?: string; children?: React.ReactNode }

/** The head of the board. Each stage has its name, the roles in it now, and one piece of a band that narrows from left to right. */
export function Funnel({ steps, counts, className, children }: Props) {
  const top = steps[0]?.reached ?? 0
  const height = (reached: number) => (top && reached ? Math.max(3, (reached / top) * BAND) : 0)
  return (
    <section aria-label="Pipeline funnel" className={className}>
      <ol className="contents">
        {steps.map((step, i) => {
          const upper = height(step.reached)
          const lower = height(steps[i + 1]?.reached ?? step.reached)
          const c = BAND / 2
          return (
            <li key={step.id} className="flex min-w-0 flex-col gap-1.5 px-2">
              <h2 className="flex items-center gap-2 font-medium">
                <StageIcon index={i} total={steps.length} />
                <span className="truncate">{step.label}</span>
                <span className="text-muted-foreground tabular-nums"><span className="sr-only">Roles here: </span>{counts[step.id] ?? 0}</span>
              </h2>
              <svg viewBox={`0 0 100 ${BAND}`} preserveAspectRatio="none" className="h-7 w-full" aria-hidden>
                <rect x="0" y={c - 0.5} width="100" height="1" className="fill-border" />
                {step.reached > 0 && <polygon className={cn(FILLS[i])} points={`0,${c - upper / 2} 100,${c - lower / 2} 100,${c + lower / 2} 0,${c + upper / 2}`} />}
              </svg>
              <p className="flex items-baseline justify-between gap-2 text-[12px] text-muted-foreground tabular-nums">
                <span>{step.reached} reached</span>
                {step.share !== null && <span>{step.share}%</span>}
              </p>
            </li>
          )
        })}
        {children}
      </ol>
    </section>
  )
}
