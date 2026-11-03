import { ChevronRight, Clock, KeyRound } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"
import { Field, Page, Panel, Problem, Status, fieldClass } from "@/components/Status"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Select } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { type Account, type Overview, type ShiftDay, addAccount, changePassword, saveShift } from "@/lib/api"
import { blankShift, shiftBody, shiftText, timeChoices } from "@/lib/shift"
import { cn } from "@/lib/utils"

const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
const message = (err: unknown) => (err instanceof Error ? err.message : "The action failed.")

export function Accounts({ data, error, onChange }: { data: Overview | null; error: string | null; onChange: () => void }) {
  const [adding, setAdding] = useState(false)
  const [password, setPassword] = useState<Account | null>(null)
  return (
    <Page title="Accounts and shifts" note="The LinkedIn accounts, their limits and their working hours."
      actions={<Button onClick={() => setAdding(true)}>Add an account</Button>}>
      <Problem text={error} />
      {data && !data.accounts.length && <p className="py-10 text-center text-sm text-muted-foreground">No LinkedIn account yet. Add an account.</p>}
      <div className="flex flex-col gap-4">
        {data?.accounts.map((account) => <AccountPanel key={account.id} account={account} onPassword={() => setPassword(account)} onChange={onChange} />)}
      </div>
      {adding && <AddAccount onClose={() => setAdding(false)} onDone={onChange} />}
      {password && <PasswordDialog account={password} onClose={() => setPassword(null)} />}
    </Page>
  )
}

function AccountPanel({ account, onPassword, onChange }: { account: Account; onPassword: () => void; onChange: () => void }) {
  const [open, setOpen] = useState(false)
  const used = account.max_attempts_24h ? Math.min(1, account.attempts_24h / account.max_attempts_24h) : 0
  return (
    <Panel label={account.label}>
      <div className="flex flex-wrap items-start gap-x-6 gap-y-3 px-4 py-3.5">
        <div className="min-w-0 flex-1 basis-64">
          <p className="flex items-center gap-2">
            <span className="truncate font-medium">{account.label}</span>
            <Status status={account.state} tone={account.state === "active" || account.state === "enabled" ? "good" : "idle"} />
          </p>
          {account.attention_detail && <p className="mt-1.5 text-sm text-amber-700 dark:text-amber-300">{account.attention_detail}</p>}
        </div>
        <div className="w-56 shrink-0">
          <p className="flex justify-between text-xs text-muted-foreground">
            <span>Scrapes in 24 hours</span>
            <span className="text-foreground tabular-nums">{account.attempts_24h} of {account.max_attempts_24h}</span>
          </p>
          <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-muted" role="presentation">
            <div className={cn("h-full rounded-full", used >= 1 ? "bg-amber-500" : "bg-violet-500")} style={{ width: `${used * 100}%` }} />
          </div>
          <p className="mt-1.5 text-xs text-muted-foreground">{account.min_interval_seconds}s between scrapes</p>
        </div>
        <Button variant="outline" size="sm" onClick={onPassword}><KeyRound aria-hidden /> Change password</Button>
      </div>
      <button type="button" aria-expanded={open} onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2.5 px-4 py-2.5 text-left text-sm outline-none transition-colors hover:bg-muted/50 focus-visible:bg-muted/50">
        <ChevronRight className={cn("size-4 shrink-0 text-muted-foreground transition-transform", open && "rotate-90")} aria-hidden />
        <Clock className="size-4 shrink-0 text-muted-foreground" aria-hidden />
        <span className="font-medium">Shift</span>
        <span className="min-w-0 flex-1 truncate text-muted-foreground">{shiftText(account.schedule)}</span>
        <span className="hidden text-xs text-muted-foreground sm:inline">{open ? "Close" : "Change"}</span>
      </button>
      {open && <ShiftEditor account={account} onClose={() => setOpen(false)} onDone={onChange} />}
    </Panel>
  )
}

function ShiftEditor({ account, onClose, onDone }: { account: Account; onClose: () => void; onDone: () => void }) {
  const [shift, setShift] = useState(account.schedule ?? blankShift())
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const day = (weekday: number, change: Partial<ShiftDay>) =>
    setShift({ ...shift, days: shift.days.map((item) => (item.weekday === weekday ? { ...item, ...change } : item)) })
  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setSaving(true)
    try {
      await saveShift(account.id, shiftBody(shift))
      toast.success("Saved the shift.")
      onDone()
      onClose()
    } catch (err) {
      setError(message(err))
    }
    setSaving(false)
  }
  return (
    <form onSubmit={submit} aria-label={`Shift of ${account.label}`} className="flex flex-col gap-4 bg-muted/20 px-4 py-4 sm:pl-11">
      <Problem text={error} />
      <div className="flex flex-wrap items-end gap-x-6 gap-y-3">
        <div className="w-64"><Field label="Time zone"><input required value={shift.timezone} onChange={(e) => setShift({ ...shift, timezone: e.target.value })} className={fieldClass} /></Field></div>
        <label className="flex h-9 items-center gap-2 text-sm">
          <Switch checked={shift.enabled} onChange={(enabled) => setShift({ ...shift, enabled })} />
          {shift.enabled ? "The shift is on" : "The shift is off. The account does not scrape."}
        </label>
      </div>
      <div className={cn("overflow-x-auto", !shift.enabled && "opacity-50")}>
        <table className="text-sm">
          <thead>
            <tr className="text-left text-xs text-muted-foreground">
              <th className="w-32 pb-1.5 font-normal">Day</th>
              <th className="w-32 pb-1.5 font-normal">Start</th>
              <th className="w-32 pb-1.5 font-normal">End</th>
              <th className="w-32 pb-1.5 font-normal">Lunch</th>
            </tr>
          </thead>
          <tbody>
            {shift.days.map((item) => {
              const name = DAYS[item.weekday - 1]
              return (
                <tr key={item.weekday} className="h-11 border-t">
                  <td>
                    <label className="flex items-center gap-2.5">
                      <Checkbox checked={item.working} disabled={!shift.enabled}
                        onChange={(working) => day(item.weekday, working ? { working: true, start_local: "10:00", end_local: "19:00" } : { working: false, start_local: null, end_local: null })} />
                      {name.slice(0, 3)}
                    </label>
                  </td>
                  {item.working ? (
                    <>
                      <td className="pr-2"><TimeSelect label={`${name} start`} value={item.start_local} disabled={!shift.enabled} onChange={(start_local) => day(item.weekday, { start_local })} /></td>
                      <td className="pr-2"><TimeSelect label={`${name} end`} value={item.end_local} disabled={!shift.enabled} onChange={(end_local) => day(item.weekday, { end_local })} /></td>
                      <td>
                        <label className="flex items-center gap-2 text-muted-foreground">
                          <input type="number" min={0} max={240} aria-label={`${name} lunch minutes`} disabled={!shift.enabled} value={item.lunch_minutes}
                            onChange={(e) => day(item.weekday, { lunch_minutes: Number(e.target.value) })} className={cn(fieldClass, "h-8 w-16 text-foreground tabular-nums")} />
                          min
                        </label>
                      </td>
                    </>
                  ) : <td colSpan={3} className="text-muted-foreground">Day off</td>}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <div className="flex gap-1.5">
        <Button type="submit" size="sm" disabled={saving}>Save the shift</Button>
        <Button type="button" size="sm" variant="ghost" onClick={onClose}>Cancel</Button>
      </div>
    </form>
  )
}

function TimeSelect({ label, value, onChange, disabled }: { label: string; value: string | null; onChange: (value: string) => void; disabled?: boolean }) {
  return <Select label={label} value={value?.slice(0, 5) ?? ""} onChange={onChange} disabled={disabled} className="h-8 w-28 tabular-nums"
    choices={timeChoices(value).map((time) => ({ value: time, label: time }))} />
}

function AddAccount({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [form, setForm] = useState({ label: "", username: "", password: "", max_attempts_24h: "20", min_interval_seconds: "300" })
  const [error, setError] = useState<string | null>(null)
  const set = (key: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [key]: e.target.value })
  async function submit(e: React.FormEvent) {
    e.preventDefault()
    try {
      await addAccount({ ...form, max_attempts_24h: Number(form.max_attempts_24h), min_interval_seconds: Number(form.min_interval_seconds) })
      toast.success("Added the account.")
      onDone()
      onClose()
    } catch (err) {
      setError(message(err))
    }
  }
  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose() }}>
      <DialogContent>
        <form onSubmit={submit} className="flex flex-col gap-3">
          <DialogHeader><DialogTitle>Add a LinkedIn account</DialogTitle><DialogDescription>The server encrypts the password. Nobody can read it back.</DialogDescription></DialogHeader>
          <Problem text={error} />
          <Field label="Name in this console"><input required value={form.label} onChange={set("label")} className={fieldClass} /></Field>
          <Field label="LinkedIn username"><input required minLength={3} autoComplete="off" value={form.username} onChange={set("username")} className={fieldClass} /></Field>
          <Field label="LinkedIn password"><input required type="password" autoComplete="new-password" value={form.password} onChange={set("password")} className={fieldClass} /></Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Scrapes in 24 hours"><input required type="number" min={1} value={form.max_attempts_24h} onChange={set("max_attempts_24h")} className={fieldClass} /></Field>
            <Field label="Seconds between scrapes"><input required type="number" min={0} value={form.min_interval_seconds} onChange={set("min_interval_seconds")} className={fieldClass} /></Field>
          </div>
          <DialogFooter><Button type="submit">Add the account</Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function PasswordDialog({ account, onClose }: { account: Account; onClose: () => void }) {
  const [password, setPassword] = useState("")
  const [error, setError] = useState<string | null>(null)
  async function submit(e: React.FormEvent) {
    e.preventDefault()
    try {
      await changePassword(account.id, password)
      toast.success("Changed the password.")
      onClose()
    } catch (err) {
      setError(message(err))
    }
  }
  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose() }}>
      <DialogContent>
        <form onSubmit={submit} className="flex flex-col gap-3">
          <DialogHeader><DialogTitle>Change the password of {account.label}</DialogTitle><DialogDescription>Use this when the LinkedIn password changes.</DialogDescription></DialogHeader>
          <Problem text={error} />
          <Field label="New LinkedIn password"><input required type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} className={fieldClass} /></Field>
          <DialogFooter><Button type="submit" disabled={!password}>Change password</Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
