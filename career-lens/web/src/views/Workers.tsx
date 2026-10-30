import { useState } from "react"
import { toast } from "sonner"
import { Field, Page, Problem, Status, fieldClass } from "@/components/Status"
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter,
  AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Select } from "@/components/ui/select"
import { type Overview, type Worker, assign, assignmentCommand, patchGoogle } from "@/lib/api"
import { ago, words } from "@/lib/format"

const COMMANDS: { id: string; label: string; warning?: string }[] = [
  { id: "verify", label: "Verify" },
  { id: "retry-login", label: "Retry sign-in" },
  { id: "confirm-replace", label: "Replace the signed-in account", warning: "This puts this account in the worker's Chrome in place of the account that is signed in now." },
  { id: "clear", label: "Clear the assignment", warning: "This removes the account from this worker. The worker stops scrapes for this account." },
]

type Props = { data: Overview | null; error: string | null; onChange: () => void }

export function Workers({ data, error, onChange }: Props) {
  return (
    <Page title="Workers" note="The native workers on your machine, and the account that each one uses.">
      <Problem text={error} />
      {data && !data.workers.length && <p className="py-10 text-center text-sm text-muted-foreground">No worker has started. Run: systemctl --user start career-lens-worker</p>}
      <div className="flex flex-col gap-3">
        {data?.workers.map((worker) => <WorkerRow key={worker.id} worker={worker} data={data} onChange={onChange} />)}
      </div>
    </Page>
  )
}

function WorkerRow({ worker, data, onChange }: { worker: Worker; data: Overview; onChange: () => void }) {
  const [asked, setAsked] = useState<(typeof COMMANDS)[number] | null>(null)
  const [accountId, setAccountId] = useState("")
  const assignment = worker.assignment
  const account = data.accounts.find((item) => item.id === assignment?.account_id)
  const free = data.accounts.filter((item) => !item.assignment)

  async function act(action: () => Promise<unknown>, done: string) {
    try {
      await action()
      toast.success(done)
      onChange()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "The action failed.")
    }
  }

  return (
    <section className="rounded-xl border p-4" aria-label={worker.name}>
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="font-medium">{worker.name}</h2>
        <Status status={worker.live ? "live" : "offline"} tone={worker.live ? "good" : "bad"} />
        {worker.health !== "unknown" && <Status status={worker.health} tone="idle" />}
        <span className="ml-auto text-xs text-muted-foreground">{worker.supervisor}{worker.last_heartbeat_at ? `, seen ${ago(worker.last_heartbeat_at)}` : ""}</span>
      </div>
      {worker.health_detail && <p className="mt-1 text-sm text-muted-foreground">{worker.health_detail}</p>}
      {assignment ? (
        <div className="mt-3 text-sm">
          <p>Account: <b className="font-medium">{account?.label ?? assignment.account_id}</b>, {words(assignment.state).toLowerCase()}</p>
          {assignment.detail && <p className="mt-0.5 text-muted-foreground">{assignment.detail}</p>}
          {assignment.waiting_for_you && <p className="mt-1.5 rounded-lg bg-amber-500/12 px-2.5 py-1.5 text-amber-800 dark:text-amber-200">{assignment.action}</p>}
          <div className="mt-2.5 flex flex-wrap gap-1.5">
            {COMMANDS.map((command) => (
              <Button key={command.id} variant="outline" size="sm" onClick={() => command.warning ? setAsked(command) : void act(() => assignmentCommand(assignment.id, command.id), `Sent: ${command.label}.`)}>
                {command.label}
              </Button>
            ))}
          </div>
        </div>
      ) : (
        <form className="mt-3 flex flex-col gap-2 sm:flex-row sm:items-end" onSubmit={(e) => { e.preventDefault(); void act(() => assign(worker.id, accountId), "Assigned the account.") }}>
          <Field label="Assign an account">
            <Select label="Assign an account" placeholder="Choose an account" value={accountId} onChange={setAccountId} className="w-full sm:w-64"
              choices={free.map((item) => ({ value: item.id, label: item.label }))} />
          </Field>
          <Button type="submit" variant="outline" disabled={!accountId}>Assign</Button>
        </form>
      )}
      {worker.google && <Google key={JSON.stringify(worker.google)} worker={worker} act={act} />}
      <AlertDialog open={asked !== null} onOpenChange={(open) => { if (!open) setAsked(null) }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{asked?.label}?</AlertDialogTitle>
            <AlertDialogDescription>{asked?.warning}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep it</AlertDialogCancel>
            <AlertDialogAction onClick={() => { if (asked && assignment) void act(() => assignmentCommand(assignment.id, asked.id), `Sent: ${asked.label}.`) }}>{asked?.label}</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  )
}

function Google({ worker, act }: { worker: Worker; act: (action: () => Promise<unknown>, done: string) => Promise<void> }) {
  const policy = worker.google!.policy
  const [hour, setHour] = useState(String(policy.max_searches_hour))
  const [day, setDay] = useState(String(policy.max_searches_24h))
  return (
    <form className="mt-3 border-t pt-3" onSubmit={(e) => { e.preventDefault(); void act(() => patchGoogle(worker.id, { max_searches_hour: Number(hour), max_searches_24h: Number(day) }), "Saved the Google limits.") }}>
      <div className="flex flex-wrap items-end gap-2">
        <label className="flex h-9 items-center gap-2 text-sm">
          <Checkbox checked={policy.enabled} onChange={(enabled) => void act(() => patchGoogle(worker.id, { enabled }), enabled ? "Google search is on." : "Google search is off.")} />
          Google search
        </label>
        <div className="w-28"><Field label="Each hour"><input type="number" min={1} value={hour} onChange={(e) => setHour(e.target.value)} className={fieldClass} /></Field></div>
        <div className="w-28"><Field label="Each 24 hours"><input type="number" min={1} value={day} onChange={(e) => setDay(e.target.value)} className={fieldClass} /></Field></div>
        <Button type="submit" variant="outline">Save limits</Button>
      </div>
    </form>
  )
}
