import { useEffect, useState } from "react"
import { toast } from "sonner"
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter,
  AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { deleteGoal, deleteMandate, goalDeletePreview, mandateDeletePreview, type DeletePreview } from "@/lib/api"

export type DeleteTarget = { kind: "goal" | "mandate"; id: string; title: string }

export function DeleteDialog({ target, onClose, onDeleted }: { target: DeleteTarget | null; onClose: () => void; onDeleted: (target: DeleteTarget) => void }) {
  const [preview, setPreview] = useState<DeletePreview | null>(null)
  useEffect(() => {
    setPreview(null)
    if (!target) return
    (target.kind === "goal" ? goalDeletePreview : mandateDeletePreview)(target.id).then(setPreview).catch(() => {})
  }, [target])
  const remove = async () => {
    if (!target) return
    try {
      await (target.kind === "goal" ? deleteGoal : deleteMandate)(target.id)
      onDeleted(target)
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err))
      onClose()
    }
  }
  return (
    <AlertDialog open={target !== null} onOpenChange={(open) => { if (!open) onClose() }}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete {target?.title}?</AlertDialogTitle>
          <AlertDialogDescription>
            {preview ? `This deletes ${preview.goals} goal${preview.goals === 1 ? "" : "s"}, ${preview.jobs} role${preview.jobs === 1 ? "" : "s"} and ${preview.notes} application note${preview.notes === 1 ? "" : "s"}. ` : ""}
            {preview?.working ? "The agents that work on it stop first. " : ""}Roles in the Job pipeline and their notes stay. You cannot undo this.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Keep it</AlertDialogCancel>
          <AlertDialogAction onClick={() => void remove()}>Delete</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
