import { useState } from "react"
import { toast } from "sonner"
import { Field, Page, Problem, Status, fieldClass } from "@/components/Status"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { type Account, type Overview, type ShiftDay, addAccount, changePassword, saveShift } from "@/lib/api"
import { blankShift, shiftBody, shiftText } from "@/lib/shift"

const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
const message = (err: unknown) => (err instanceof Error ? err.message : "The action failed.")

export function Accounts({ data, error, onChange }: { data: Overview | null; error: string | null; onChange: () => void }) {
  const [adding, setAdding] = useState(false)
  const [password, setPassword] = useState<Account | null>(null)
  const [shift, setShift] = useState<Account | null>(null)
  return (
    <Page title="Accounts and shifts" note="The LinkedIn accounts, their limits and their working hours."
      actions={<Button variant="outline" onClick={() => setAdding(true)}>Add an account</Button>}>
      <Problem text={error} />
      {data && !data.accounts.length && <p className="py-10 text-center text-sm text-muted-foreground">No LinkedIn account yet. Add an account.</p>}
      <ul className="divide-y rounded-xl border empty:hidden">
        {data?.accounts.map((account) => (
          <li key={account.id} className="flex flex-wrap items-center gap-x-3 gap-y-2 px-3 py-3">
            <div className="min-w-0 flex-1 basis-64">
              <p className="flex items-center gap-2"><span className="truncate font-medium">{account.label}</span><Status status={account.state} tone={account.state === "active" ? "good" : "idle"} /></p>
              <p className="text-sm text-muted-foreground">{account.attempts_24h} of {account.max_attempts_24h} scrapes in 24 hours, {account.min_interval_seconds}s between scrapes</p>
              <p className="text-sm text-muted-foreground">{shiftText(account.schedule)}</p>
              {account.attention_detail && <p className="mt-1 text-sm text-amber-700 dark:text-amber-300">{account.attention_detail}</p>}
            </div>
            <div className="flex gap-1.5">
              <Button variant="outline" size="sm" onClick={() => setShift(account)}>Change shift</Button>
              <Button variant="outline" size="sm" onClick={() => setPassword(account)}>Change password</Button>
            </div>
          </li>
        ))}
      </ul>
      {adding && <AddAccount onClose={() => setAdding(false)} onDone={onChange} />}
      {password && <PasswordDialog account={password} onClose={() => setPassword(null)} />}
      {shift && <ShiftDialog account={shift} onClose={() => setShift(null)} onDone={onChange} />}
    </Page>
  )
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

function ShiftDialog({ account, onClose, onDone }: { account: Account; onClose: () => void; onDone: () => void }) {
  const [shift, setShift] = useState(account.schedule ?? blankShift())
  const [error, setError] = useState<string | null>(null)
  const day = (weekday: number, change: Partial<ShiftDay>) =>
    setShift({ ...shift, days: shift.days.map((item) => (item.weekday === weekday ? { ...item, ...change } : item)) })
  async function submit(e: React.FormEvent) {
    e.preventDefault()
    try {
      await saveShift(account.id, shiftBody(shift))
      toast.success("Saved the shift.")
      onDone()
      onClose()
    } catch (err) {
      setError(message(err))
    }
  }
  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose() }}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-xl">
        <form onSubmit={submit} className="flex flex-col gap-3">
          <DialogHeader><DialogTitle>Shift of {account.label}</DialogTitle><DialogDescription>The account scrapes only in these hours.</DialogDescription></DialogHeader>
          <Problem text={error} />
          <div className="flex items-end gap-3">
            <div className="flex-1"><Field label="Time zone"><input required value={shift.timezone} onChange={(e) => setShift({ ...shift, timezone: e.target.value })} className={fieldClass} /></Field></div>
            <label className="flex h-9 items-center gap-2 text-sm"><input type="checkbox" checked={shift.enabled} onChange={(e) => setShift({ ...shift, enabled: e.target.checked })} />Shift is on</label>
          </div>
          <ul className="flex flex-col gap-1.5">
            {shift.days.map((item) => (
              <li key={item.weekday} className="grid grid-cols-[6.5rem_1fr_1fr_4.5rem] items-center gap-2 text-sm">
                <label className="flex items-center gap-2"><input type="checkbox" checked={item.working} onChange={(e) => day(item.weekday, e.target.checked ? { working: true, start_local: "10:00", end_local: "19:00" } : { working: false, start_local: null, end_local: null })} />{DAYS[item.weekday - 1].slice(0, 3)}</label>
                <input type="time" aria-label={`${DAYS[item.weekday - 1]} start`} disabled={!item.working} value={item.start_local?.slice(0, 5) ?? ""} onChange={(e) => day(item.weekday, { start_local: e.target.value })} className={fieldClass} />
                <input type="time" aria-label={`${DAYS[item.weekday - 1]} end`} disabled={!item.working} value={item.end_local?.slice(0, 5) ?? ""} onChange={(e) => day(item.weekday, { end_local: e.target.value })} className={fieldClass} />
                <input type="number" min={0} max={240} aria-label={`${DAYS[item.weekday - 1]} lunch minutes`} title="Lunch minutes" disabled={!item.working} value={item.lunch_minutes} onChange={(e) => day(item.weekday, { lunch_minutes: Number(e.target.value) })} className={fieldClass} />
              </li>
            ))}
          </ul>
          <p className="text-xs text-muted-foreground">The last column is the lunch in minutes.</p>
          <DialogFooter><Button type="submit">Save the shift</Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
