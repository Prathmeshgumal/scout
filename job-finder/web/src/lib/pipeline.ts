import type { PipelineItem, PipelineStage } from "./api"

export const STAGES: { id: PipelineStage; label: string }[] = [
  { id: "fit", label: "Fit jobs" },
  { id: "referral", label: "Looking for referral" },
  { id: "applied", label: "Applied" },
  { id: "responded", label: "Responded" },
]

export const STAGE_CHOICES = STAGES.map((stage) => ({ value: stage.id, label: stage.label }))

export type FunnelStep = { id: PipelineStage; label: string; reached: number; share: number | null }

/** Each stage counts the roles that reached it: a role in Applied also passed the two stages above. */
export function funnelOf(items: Pick<PipelineItem, "stage">[]): FunnelStep[] {
  const order = STAGES.map((stage) => stage.id)
  const total = items.length
  return STAGES.map((stage, i) => {
    const reached = items.filter((item) => order.indexOf(item.stage) >= i).length
    return { id: stage.id, label: stage.label, reached, share: i === 0 || !total ? null : Math.round((reached / total) * 100) }
  })
}

/** Ask "Did you apply?" only before the role reaches Applied. */
export const canAskApplied = (stage: PipelineStage | null) => stage === null || stage === "fit" || stage === "referral"
