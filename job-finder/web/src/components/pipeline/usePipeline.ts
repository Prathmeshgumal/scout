import { useCallback, useEffect, useRef, useState } from "react"
import { toast } from "sonner"
import { getJob, getPipeline, setPipelineStage, type JobDetail, type PipelineItem, type PipelineStage, type RemovedItem } from "@/lib/api"

const message = (err: unknown) => (err instanceof Error ? err.message : "The change failed.")

export type MovedRole = { role_key: string; title: string; stage: PipelineStage | "removed" | null }

/** The roles in the pipeline. The data stays current, and a move shows on the screen before the server answers. */
export function usePipeline() {
  const [board, setBoard] = useState<{ items: PipelineItem[]; removed: RemovedItem[] } | null>(null)
  const [failed, setFailed] = useState(false)
  const paused = useRef(false)
  // A read that started before the latest move has old data: it must not undo the move on the screen.
  const changes = useRef(0)

  const read = useCallback(async () => {
    const started = changes.current
    try {
      const body = await getPipeline()
      if (started !== changes.current || paused.current) return
      setBoard({ items: body.items, removed: body.removed ?? [] })
      setFailed(false)
    } catch {
      setFailed(true)
    }
  }, [])

  useEffect(() => {
    void read()
    const timer = setInterval(() => { if (!paused.current) void read() }, 10_000)
    return () => clearInterval(timer)
  }, [read])

  const move = useCallback(async function move(role: MovedRole, stage: PipelineStage | "removed"): Promise<void> {
    changes.current += 1
    setBoard((now) => {
      if (!now) return now
      const row = [...now.items, ...now.removed].find((one) => one.role_key === role.role_key)
      const items = now.items.filter((one) => one.role_key !== role.role_key)
      const removed = now.removed.filter((one) => one.role_key !== role.role_key)
      if (!row) return now
      return stage === "removed" ? { items, removed: [{ ...row, stage }, ...removed] } : { items: [{ ...row, stage }, ...items], removed }
    })
    try {
      await setPipelineStage(role.role_key, stage)
      if (stage === "removed" && role.stage && role.stage !== "removed") {
        const before = role.stage
        toast.success(`Removed ${role.title} from the pipeline.`, {
          action: { label: "Undo", onClick: () => void move({ ...role, stage: null }, before) },
        })
      }
    } catch (err) {
      toast.error(message(err))
    }
    await read()
  }, [read])

  const pause = useCallback((now: boolean) => { paused.current = now }, [])

  return { items: board?.items ?? null, removed: board?.removed ?? null, failed, read, move, pause }
}

/** The full details of a role. A role whose goal is deleted has none. */
export function useJobDetail(goalId: string | null, jobId: string | null) {
  const [detail, setDetail] = useState<JobDetail | null>(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    if (!goalId || !jobId) return
    let active = true
    getJob(goalId, jobId).then((d) => { if (active) setDetail(d) }).catch(() => { if (active) setFailed(true) })
    return () => { active = false }
  }, [goalId, jobId])
  return { detail, failed }
}
