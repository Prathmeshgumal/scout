import type { FunnelStep } from "@/lib/pipeline"
import { cn } from "@/lib/utils"

const WIDTH = 120
const BAND = 44
const GAP = 2
// One hue that deepens stage by stage. The text beside each band gives the name and the count.
const FILLS = [
  "fill-violet-300 dark:fill-violet-800",
  "fill-violet-400 dark:fill-violet-700",
  "fill-violet-500 dark:fill-violet-500",
  "fill-violet-700 dark:fill-violet-300",
]

/** The funnel of the pipeline. A row shows the count, the share of the first stage and the name. */
export function Funnel({ steps }: { steps: FunnelStep[] }) {
  const top = steps[0]?.reached ?? 0
  const width = (reached: number) => (top ? Math.max(reached ? 6 : 0, (reached / top) * WIDTH) : 0)
  return (
    <section aria-label="Pipeline funnel" className="rounded-xl border bg-card p-4">
      <h2 className="font-semibold">Funnel</h2>
      {top === 0 ? <p className="mt-2 text-muted-foreground">The funnel appears when the pipeline has a role.</p> : (
        <>
          <div className="mt-3 flex gap-3">
            <svg width={WIDTH} height={steps.length * (BAND + GAP) - GAP} className="shrink-0" aria-hidden>
              {steps.map((step, i) => {
                const upper = width(step.reached)
                const lower = width(steps[i + 1]?.reached ?? step.reached)
                const y = i * (BAND + GAP)
                const c = WIDTH / 2
                return (
                  <polygon key={step.id} className={cn(FILLS[i], "transition-opacity hover:opacity-80")}
                    points={`${c - upper / 2},${y} ${c + upper / 2},${y} ${c + lower / 2},${y + BAND} ${c - lower / 2},${y + BAND}`}>
                    <title>{`${step.label}: ${step.reached}`}</title>
                  </polygon>
                )
              })}
            </svg>
            <ol className="flex min-w-0 flex-1 flex-col gap-[2px]">
              {steps.map((step) => (
                <li key={step.id} className="flex h-[44px] flex-col justify-center">
                  <span className="flex items-baseline gap-1.5">
                    <span className="text-[17px] leading-tight font-semibold tabular-nums">{step.reached}</span>
                    {step.share !== null && <span className="text-[12px] text-muted-foreground tabular-nums">{step.share}%</span>}
                  </span>
                  <span className="truncate text-[12px] text-muted-foreground" title={step.label}>{step.label}</span>
                </li>
              ))}
            </ol>
          </div>
          <p className="mt-3 text-[12px] leading-snug text-muted-foreground">Each stage counts the roles that reached it or a later stage.</p>
        </>
      )}
    </section>
  )
}
