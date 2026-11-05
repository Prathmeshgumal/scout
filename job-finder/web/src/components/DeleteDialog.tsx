import { useEffect, useState } from "react"
import { toast } from "sonner"
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter,
  AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { deleteGoal, deleteMandate, goalDeletePreview, mandateDeletePreview, type DeletePreview } from "@/lib/api"
import { deleteText } from "@/lib/delete"

export type DeleteTarget = { kind: "goal" | "mandate"; id: string; title: string }

export function DeleteDialog({ target, onClose, onDeleted }: { target: DeleteTarget | null; onClose: () => void; onDeleted: (target: DeleteTarget) => void }) {
  const [preview, setPreview] = useState<DeletePreview | null>(null)
  useEffect(() => {
    setPreview(null)
    if (!target) return
    let live = true
    ;(target.kind === "goal" ? goalDeletePreview : mandateDeletePreview)(target.id).then((value) => {
      if (live) setPreview(value)
    }).catch((err: unknown) => {
      if (!live) return
      toast.error(err instanceof Error ? err.message : String(err))
      onClose()
    })
    return () => { live = false }
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
          <AlertDialogDescription>{target && preview ? deleteText(target.kind, preview) : "Reading what this deletes"}</AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Keep it</AlertDialogCancel>
          <AlertDialogAction variant="destructive" disabled={!preview} onClick={() => void remove()}>Delete</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
