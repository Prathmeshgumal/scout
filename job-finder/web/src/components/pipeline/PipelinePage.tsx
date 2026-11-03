import { Archive, ChevronLeft } from "lucide-react"
import { useState } from "react"
import { SkeletonRows } from "@/components/jobs/JobRow"
import { Button } from "@/components/ui/button"
import type { PipelineItem, PipelineStage, RemovedItem } from "@/lib/api"
import { useBoardOrder } from "@/components/board/useBoardOrder"
import { PipelineBoard } from "./PipelineBoard"
import { RolePage } from "./RolePage"
import { useJobDetail, usePipeline } from "./usePipeline"

type Props = {
  /** The role whose page is open, or null for the board. */
  roleKey: string | null
  /** The card to show on the board. */
  highlight?: string | null
  onOpenRole: (roleKey: string) => void
  onBoard: () => void
  onOpenGoal: (id: string) => void
  onJobs: () => void
}

type RoleProps = {
  item: PipelineItem | RemovedItem
  draft: string
  onDraft: (text: string) => void
  onBack: () => void
  onMove: (stage: PipelineStage | "removed") => void
  onChanged: () => void
  onOpenGoal: (id: string) => void
}

function PipelineRole({ item, ...rest }: RoleProps) {
  const { detail, failed } = useJobDetail(item.goal_id, item.job_id)
  return <RolePage role={item} roleKey={item.role_key} stage={item.stage === "removed" ? null : item.stage} stageKnown notes={item.notes} detail={detail} failed={failed} backLabel="Job pipeline" {...rest} />
}

/** The roles that the user tracks: a board of stages, and a page for each role. */
export function PipelinePage({ roleKey, highlight = null, onOpenRole, onBoard, onOpenGoal, onJobs }: Props) {
  const { items, removed, failed, read, move, pause } = usePipeline()
  const order = useBoardOrder("pipeline")
  const [showRemoved, setShowRemoved] = useState(false)
  const removedCount = removed?.length ?? 0
  // A note that is not saved stays when the user goes back to the board.
  const [drafts, setDrafts] = useState<Record<string, string>>({})

  if (roleKey) {
    const item = [...(items ?? []), ...(removed ?? [])].find((row) => row.role_key === roleKey)
    if (item) {
      return (
        <PipelineRole key={roleKey} item={item} draft={drafts[roleKey] ?? ""} onDraft={(text) => setDrafts((now) => ({ ...now, [roleKey]: text }))}
          onBack={onBoard} onChanged={() => void read()} onOpenGoal={onOpenGoal}
          onMove={(stage) => { if (stage === "removed") onBoard(); void move(item, stage) }} />
      )
    }
    return (
      <div className="flex flex-col items-start gap-3 p-5">
        {!items && !failed && <SkeletonRows count={3} />}
        {failed && !items && <p className="text-red-700 dark:text-red-300">Could not read the pipeline. The page tries again in 10 seconds.</p>}
        {items && <p className="text-muted-foreground">The pipeline does not have this role.</p>}
        <Button size="sm" variant="outline" onClick={onBoard}><ChevronLeft aria-hidden /> Job pipeline</Button>
      </div>
    )
  }

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      <header className="flex shrink-0 items-start justify-between gap-4 px-6 pt-4">
        <div className="min-w-0">
          <h1 className="text-[17px] font-semibold">Job pipeline</h1>
          <p className="text-muted-foreground">Drag a role to change its stage, or up and down to change its priority.</p>
        </div>
        {(removedCount > 0 || showRemoved) && (
          <Button variant={showRemoved ? "secondary" : "outline"} size="sm" aria-pressed={showRemoved} onClick={() => setShowRemoved(!showRemoved)}>
            <Archive aria-hidden /> Removed <span className="tabular-nums opacity-70">{removedCount}</span>
          </Button>
        )}
      </header>
      {failed && !items && <p className="px-6 py-3 text-red-700 dark:text-red-300">Could not read the pipeline. The page tries again in 10 seconds.</p>}
      {!items && !failed && <div className="p-4"><SkeletonRows count={4} /></div>}
      {items?.length === 0 && removedCount === 0 && (
        <div className="flex flex-col items-start gap-2 px-6 py-6">
          <p className="font-medium">The pipeline has no roles.</p>
          <p className="text-muted-foreground">A role comes here when an agent marks it as a fit, or when you choose a stage in its details.</p>
          <Button size="sm" variant="outline" onClick={onJobs}>Open Jobs</Button>
        </div>
      )}
      {items && (items.length > 0 || removedCount > 0) && (
        <PipelineBoard items={items} removed={showRemoved ? removed ?? [] : null} highlight={highlight} onOpen={onOpenRole} onDragging={pause} positions={order.positions}
          onMove={(item, stage, keys) => { if (stage !== item.stage) void move(item, stage); void order.save(stage, keys) }} />
      )}
    </div>
  )
}
