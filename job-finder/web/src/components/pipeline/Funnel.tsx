import type { FunnelStep } from "@/lib/pipeline"
import { cn } from "@/lib/utils"

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

const WIDTH = 108
const STEP = 46
const GAP = 3

/** The funnel of the pipeline, beside the board. A row shows the roles that reached the stage, the share of the first stage, and the name. */
export function Funnel({ steps, className }: { steps: FunnelStep[]; className?: string }) {
  const top = steps[0]?.reached ?? 0
  const width = (reached: number) => (top ? Math.max(reached ? 8 : 0, (reached / top) * WIDTH) : 0)
  return (
    <section aria-label="Pipeline funnel" className={className}>
      <h2 className="font-semibold">Funnel</h2>
      {top === 0 ? <p className="mt-2 text-muted-foreground">The funnel appears when the pipeline has a role.</p> : (
        <>
          <div className="mt-4 flex gap-3">
            <svg width={WIDTH} height={steps.length * (STEP + GAP) - GAP} className="shrink-0" aria-hidden>
              {steps.map((step, i) => {
                const upper = width(step.reached)
                const lower = width(steps[i + 1]?.reached ?? step.reached)
                const y = i * (STEP + GAP)
                const c = WIDTH / 2
                return (
                  <polygon key={step.id} className={cn(FILLS[i], "transition-[points] duration-300")}
                    points={`${c - upper / 2},${y} ${c + upper / 2},${y} ${c + lower / 2},${y + STEP} ${c - lower / 2},${y + STEP}`}>
                    <title>{`${step.label}: ${step.reached}`}</title>
                  </polygon>
                )
              })}
            </svg>
            <ol className="flex min-w-0 flex-1 flex-col" style={{ gap: GAP }}>
              {steps.map((step) => (
                <li key={step.id} className="flex flex-col justify-center" style={{ height: STEP }}>
                  <span className="flex items-baseline gap-1.5">
                    <span className="text-[18px] leading-tight font-semibold tabular-nums">{step.reached}</span>
                    {step.share !== null && <span className="text-[12px] text-muted-foreground tabular-nums">{step.share}%</span>}
                  </span>
                  <span className="text-[12px] leading-tight text-muted-foreground">{step.label}</span>
                </li>
              ))}
            </ol>
          </div>
          <p className="mt-4 text-[12px] leading-snug text-muted-foreground">Each stage counts the roles that reached it or a later stage. Removed roles are not counted.</p>
        </>
      )}
    </section>
  )
}
