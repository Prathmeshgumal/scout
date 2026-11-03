import { ChevronDown, RefreshCw, ShieldCheck, UserRoundX, Users } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"
import { Page, Panel, Problem, SettingRow, Status, fieldClass } from "@/components/Status"
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter,
  AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Select } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { type Overview, type Worker, assign, assignmentCommand, patchGoogle } from "@/lib/api"
import { ago } from "@/lib/format"
import { cn } from "@/lib/utils"

type Command = { id: string; label: string; warning?: string }
const VERIFY: Command = { id: "verify", label: "Verify" }
const RETRY: Command = { id: "retry-login", label: "Retry sign-in" }
const REPLACE: Command = { id: "confirm-replace", label: "Replace the signed-in account", warning: "This puts this account in the worker's Chrome in place of the account that is signed in now." }
const CLEAR: Command = { id: "clear", label: "Clear the assignment", warning: "This removes the account from this worker. The worker stops scrapes for this account." }

type Act = (action: () => Promise<unknown>, done: string) => Promise<void>
type Props = { data: Overview | null; error: string | null; onChange: () => void }

export function Workers({ data, error, onChange }: Props) {
  return (
    <Page title="Workers" note="The native workers on your machine, and the account that each one uses.">
      <Problem text={error} />
      {data && !data.workers.length && <p className="py-10 text-center text-sm text-muted-foreground">No worker has started. Run: systemctl --user start career-lens-worker</p>}
      <div className="flex flex-col gap-4">
        {data?.workers.map((worker) => <WorkerPanel key={worker.id} worker={worker} data={data} onChange={onChange} />)}
      </div>
    </Page>
  )
}

function WorkerPanel({ worker, data, onChange }: { worker: Worker; data: Overview; onChange: () => void }) {
  const [asked, setAsked] = useState<Command | null>(null)
  const assignment = worker.assignment

  const act: Act = async (action, done) => {
    try {
      await action()
      toast.success(done)
      onChange()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "The action failed.")
    }
  }
  const run = (command: Command) => {
    if (!assignment) return
    if (command.warning) setAsked(command)
    else void act(() => assignmentCommand(assignment.id, command.id), `Sent: ${command.label}.`)
  }

  return (
    <Panel label={worker.name}>
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1 bg-muted/30 px-4 py-3">
        <span className={cn("size-2 shrink-0 rounded-full", worker.live ? "bg-emerald-500" : "bg-red-500")} aria-hidden />
        <h2 className="font-medium">{worker.name}</h2>
        <Status status={worker.live ? "live" : "offline"} tone={worker.live ? "good" : "bad"} />
        {worker.health !== "unknown" && <Status status={worker.health} tone="idle" />}
        <span className="ml-auto text-xs text-muted-foreground">{worker.supervisor}{worker.last_heartbeat_at ? `, seen ${ago(worker.last_heartbeat_at)}` : ""}</span>
        {worker.health_detail && <p className="w-full text-sm text-muted-foreground">{worker.health_detail}</p>}
      </header>
      <SettingRow title="LinkedIn account" note="The account that this worker signs in with.">
        {assignment ? <Assigned worker={worker} data={data} onRun={run} /> : <AssignForm worker={worker} data={data} act={act} />}
      </SettingRow>
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
    </Panel>
  )
}

function Assigned({ worker, data, onRun }: { worker: Worker; data: Overview; onRun: (command: Command) => void }) {
  const assignment = worker.assignment!
  const account = data.accounts.find((item) => item.id === assignment.account_id)
  const ready = assignment.state === "ready"
  return (
    <div className="flex flex-col gap-2.5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <div className="min-w-0 flex-1">
          <p className="flex flex-wrap items-center gap-2">
            <span className="truncate font-medium">{account?.label ?? assignment.account_id}</span>
            <Status status={assignment.state} tone={ready ? "good" : assignment.waiting_for_you ? "wait" : "idle"} />
          </p>
          {assignment.detail && <p className="mt-0.5 text-sm text-muted-foreground">{assignment.detail}</p>}
        </div>
        <div className="flex items-center gap-1.5">
          <Button variant="outline" size="sm" onClick={() => onRun(VERIFY)}><ShieldCheck aria-hidden /> {VERIFY.label}</Button>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="outline" size="sm">More <ChevronDown aria-hidden /></Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-60">
              <DropdownMenuItem onSelect={() => onRun(RETRY)}><RefreshCw /> {RETRY.label}</DropdownMenuItem>
              <DropdownMenuItem onSelect={() => onRun(REPLACE)}><Users /> {REPLACE.label}</DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem variant="destructive" onSelect={() => onRun(CLEAR)}><UserRoundX /> {CLEAR.label}</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>
      {assignment.waiting_for_you && <p className="rounded-lg bg-amber-500/12 px-3 py-2 text-sm text-amber-800 dark:text-amber-200">{assignment.action}</p>}
    </div>
  )
}

function AssignForm({ worker, data, act }: { worker: Worker; data: Overview; act: Act }) {
  const [accountId, setAccountId] = useState("")
  const free = data.accounts.filter((item) => !item.assignment)
  if (!free.length) return <p className="text-sm text-muted-foreground">No account is free. Add an account on the Accounts page.</p>
  return (
    <form className="flex flex-wrap items-center gap-2" onSubmit={(e) => { e.preventDefault(); void act(() => assign(worker.id, accountId), "Assigned the account.") }}>
      <Select label="Assign an account" placeholder="Choose an account" value={accountId} onChange={setAccountId} className="w-full sm:w-64"
        choices={free.map((item) => ({ value: item.id, label: item.label }))} />
      <Button type="submit" variant="outline" disabled={!accountId}>Assign</Button>
    </form>
  )
}

function Google({ worker, act }: { worker: Worker; act: Act }) {
  const policy = worker.google!.policy
  const [hour, setHour] = useState(String(policy.max_searches_hour))
  const [day, setDay] = useState(String(policy.max_searches_24h))
  const changed = hour !== String(policy.max_searches_hour) || day !== String(policy.max_searches_24h)
  const valid = Number(hour) >= 1 && Number(day) >= 1
  return (
    <SettingRow title="Google search" note="This worker can also do Google searches, up to these limits.">
      <form className="flex flex-wrap items-center gap-x-5 gap-y-3"
        onSubmit={(e) => { e.preventDefault(); void act(() => patchGoogle(worker.id, { max_searches_hour: Number(hour), max_searches_24h: Number(day) }), "Saved the Google limits.") }}>
        <label className="flex items-center gap-2 text-sm">
          <Switch checked={policy.enabled} onChange={(enabled) => void act(() => patchGoogle(worker.id, { enabled }), enabled ? "Google search is on." : "Google search is off.")} />
          {policy.enabled ? "On" : "Off"}
        </label>
        <Limit label="Searches each hour" suffix="each hour" value={hour} onChange={setHour} disabled={!policy.enabled} />
        <Limit label="Searches each 24 hours" suffix="each 24 hours" value={day} onChange={setDay} disabled={!policy.enabled} />
        {changed && (
          <span className="flex gap-1.5">
            <Button type="submit" size="sm" disabled={!valid}>Save limits</Button>
            <Button type="button" size="sm" variant="ghost" onClick={() => { setHour(String(policy.max_searches_hour)); setDay(String(policy.max_searches_24h)) }}>Cancel</Button>
          </span>
        )}
      </form>
    </SettingRow>
  )
}

function Limit({ label, suffix, value, onChange, disabled }: { label: string; suffix: string; value: string; onChange: (value: string) => void; disabled?: boolean }) {
  return (
    <label className={cn("flex items-center gap-2 text-sm text-muted-foreground", disabled && "opacity-50")}>
      <input type="number" min={1} aria-label={label} value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)} className={cn(fieldClass, "h-8 w-20 text-foreground tabular-nums")} />
      {suffix}
    </label>
  )
}
