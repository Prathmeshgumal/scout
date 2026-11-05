import { Check, Copy, KeyRound, Plus } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"
import { Field, Page, Panel, Problem, Status, fieldClass } from "@/components/Status"
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter,
  AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { usePoll } from "@/hooks/usePoll"
import { type ApiKey, apiKeys, createApiKey, revokeApiKey } from "@/lib/api"
import { when } from "@/lib/format"

const message = (err: unknown) => (err instanceof Error ? err.message : "The server did not answer.")

export function ApiKeys() {
  const { data, error, refresh } = usePoll(apiKeys, null)
  const [creating, setCreating] = useState(false)
  return (
    <Page title="API keys" note="Apps use these keys to call the customer API."
      actions={<Button size="sm" onClick={() => setCreating(true)}><Plus aria-hidden /> Create key</Button>}>
      <Problem text={error} />
      {data && !data.length && <p className="py-10 text-center text-sm text-muted-foreground">There are no API keys.</p>}
      {data && data.length > 0 && (
        <Panel label="API keys">
          <ul className="divide-y">
            {data.map((key) => <KeyRow key={key.id} apiKey={key} onChange={() => void refresh()} />)}
          </ul>
        </Panel>
      )}
      {creating && <CreateDialog onClose={() => setCreating(false)} onDone={() => void refresh()} />}
    </Page>
  )
}

function KeyRow({ apiKey, onChange }: { apiKey: ApiKey; onChange: () => void }) {
  const revoke = async () => {
    try {
      await revokeApiKey(apiKey.id)
      toast.success("Revoked the key.")
      onChange()
    } catch (err) {
      toast.error(message(err))
    }
  }
  const revoked = apiKey.revoked_at !== null
  return (
    <li aria-label={apiKey.name} className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3">
      <KeyRound className="size-4 shrink-0 text-muted-foreground" aria-hidden />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <h3 className="truncate text-sm font-medium">{apiKey.name}</h3>
          <Status status={revoked ? "Revoked" : "Active"} tone={revoked ? "idle" : "good"} />
        </div>
        <p className="mt-0.5 truncate text-xs text-muted-foreground">
          <span className="font-mono">{apiKey.prefix}</span>
        </p>
        <p className="mt-0.5 text-xs text-muted-foreground">
          Created {when(apiKey.created_at)}{revoked && `. Revoked ${when(apiKey.revoked_at)}`}
        </p>
      </div>
      {!revoked && (
        <AlertDialog>
          <AlertDialogTrigger asChild><Button variant="destructive" size="sm">Revoke</Button></AlertDialogTrigger>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Revoke {apiKey.name}?</AlertDialogTitle>
              <AlertDialogDescription>Apps that use this key lose access at once. You cannot undo this.</AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>Keep it</AlertDialogCancel>
              <AlertDialogAction variant="destructive" onClick={() => void revoke()}>Revoke key</AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      )}
    </li>
  )
}

function CreateDialog({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState("")
  const [token, setToken] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setSaving(true)
    setError(null)
    try {
      setToken((await createApiKey(name)).token)
      onDone()
    } catch (err) {
      setError(message(err))
    }
    setSaving(false)
  }
  // The key shows only here, so a stray click or Escape must not lose it.
  const hold = (e: Event) => { if (token) e.preventDefault() }
  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose() }}>
      <DialogContent showCloseButton={!token} onEscapeKeyDown={hold} onInteractOutside={hold}>
        {token ? <NewKey token={token} onClose={onClose} /> : (
          <form onSubmit={submit} className="flex flex-col gap-3">
            <DialogHeader>
              <DialogTitle>Create an API key</DialogTitle>
              <DialogDescription>Use the name of the app that will use this key.</DialogDescription>
            </DialogHeader>
            <Problem text={error} />
            <Field label="Name"><input required maxLength={120} autoFocus value={name} onChange={(e) => setName(e.target.value)} className={fieldClass} /></Field>
            <DialogFooter><Button type="submit" disabled={!name.trim() || saving}>Create key</Button></DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  )
}

function NewKey({ token, onClose }: { token: string; onClose: () => void }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(token)
      setCopied(true)
    } catch {
      toast.error("The copy failed. Select the key and copy it.")
    }
  }
  return (
    <div className="flex min-w-0 flex-col gap-3">
      <DialogHeader>
        <DialogTitle>Copy your new key</DialogTitle>
        <DialogDescription>Keep this key safe. You cannot see it again after you close this box.</DialogDescription>
      </DialogHeader>
      <input readOnly aria-label="New API key" value={token} onFocus={(e) => e.target.select()} className={`${fieldClass} font-mono text-xs`} />
      <DialogFooter>
        <Button variant="outline" onClick={() => void copy()}>{copied ? <Check aria-hidden /> : <Copy aria-hidden />} Copy key</Button>
        <Button onClick={onClose}>Done</Button>
      </DialogFooter>
    </div>
  )
}
