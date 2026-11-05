import { Settings } from "lucide-react"
import { Popover } from "radix-ui"
import { useState, type FormEvent } from "react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import {
  chooseLogin, finishSubscriptionLogin, getStatus, saveLogin, startSubscriptionLogin,
  type LoginMode, type LoginResult, type Status,
} from "@/lib/api"
import { cn } from "@/lib/utils"

const RESTART_LIMIT_MS = 60_000
const EDGE = 8 // the space between the popup and the edge of the window, in px
const POPUP = "z-50 w-[300px] max-w-(--radix-popover-content-available-width) origin-(--radix-popover-content-transform-origin) rounded-lg bg-popover p-2.5 text-popover-foreground shadow-md ring-1 ring-foreground/10 outline-none duration-100 data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95"
const MODES: { mode: LoginMode; label: string; done: string }[] = [
  { mode: "oauth_token", label: "Claude Code", done: "Claude Code is in use." },
  { mode: "api_key", label: "API key", done: "The API key is in use." },
]

/** What stops the popup from closing: none, a click outside it (a code is not pasted yet), or all (the app restarts). */
type Hold = "none" | "outside" | "all"

const modeInUse = (status: Status | null): LoginMode | null =>
  status?.claude_login === "API key" ? "api_key" : status?.claude_login === "Claude subscription token" ? "oauth_token" : null

/** Wait until the app runs again: it stops answering, or it answers with a new load time. */
async function waitForRestart(before: string | null | undefined, pollMs: number) {
  const end = Date.now() + RESTART_LIMIT_MS
  let wentDown = false
  while (Date.now() < end) {
    await new Promise((done) => setTimeout(done, pollMs))
    try {
      const now = await getStatus()
      if (wentDown || now.claude_account?.loaded_at !== before) return true
    } catch {
      wentDown = true
    }
  }
  return false
}

const errorText = (err: unknown) => err instanceof Error ? err.message : "The change failed."

type Props = {
  open: boolean
  onOpenChange: (open: boolean) => void
  status: Status | null
  onChanged: () => void
  restartPollMs?: number
  className?: string
  side?: "right" | "top"
}

/** The Settings button of the rail, and the small popup that it opens. A credential stays only while the popup is open. */
export function LoginPopover({ open, onOpenChange, status, onChanged, restartPollMs = 1000, className, side = "right" }: Props) {
  const [hold, setHold] = useState<Hold>("none")
  const change = (next: boolean) => {
    if (!next && hold === "all") return
    if (!next) setHold("none")
    onOpenChange(next)
  }
  return (
    <Popover.Root open={open} onOpenChange={change}>
      <Tooltip>
        <TooltipTrigger asChild>
          <Popover.Trigger aria-label="Settings" className={className}><Settings /></Popover.Trigger>
        </TooltipTrigger>
        <TooltipContent side={side}>Settings</TooltipContent>
      </Tooltip>
      <Popover.Portal>
        <Popover.Content side={side} align="end" sideOffset={8} collisionPadding={EDGE} className={POPUP}
          onInteractOutside={(event) => { if (hold !== "none") event.preventDefault() }}
          onEscapeKeyDown={(event) => { if (hold === "all") event.preventDefault() }}>
          <LoginChoice status={status} onChanged={onChanged} restartPollMs={restartPollMs} onHold={setHold} onDone={() => change(false)} />
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  )
}

function LoginChoice({ status, onChanged, restartPollMs, onHold, onDone }: {
  status: Status | null; onChanged: () => void; restartPollMs: number; onHold: (hold: Hold) => void; onDone: () => void
}) {
  const inUse = modeInUse(status)
  const saved = status?.claude_account?.saved
  const [mode, setMode] = useState<LoginMode>(inUse ?? "oauth_token")
  const [url, setUrl] = useState<string | null>(null)
  const [code, setCode] = useState("")
  const [apiKey, setApiKey] = useState("")
  const [busy, setBusy] = useState(false)
  const [restarting, setRestarting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(status?.claude_account?.restart_required ? "Restart the app to use the saved login." : null)

  /** Do an action that changes the login. Then wait for the restart. Returns false if the action failed. */
  async function apply(action: () => Promise<LoginResult>, target: LoginMode) {
    setBusy(true); setError(null); setNotice(null)
    onHold("all")
    try {
      const result = await action()
      setCode(""); setApiKey(""); setUrl(null) // Do not keep a credential in the page after the server has it.
      const done = MODES.find((m) => m.mode === target)!.done
      if (!result.restarting) {
        setNotice("Saved. Restart job-finder to use this login.")
        onHold("none")
        onChanged()
        return true
      }
      setRestarting(true)
      if (await waitForRestart(status?.claude_account?.loaded_at, restartPollMs)) {
        toast.success(done)
        onChanged()
        onHold("none")
        onDone()
      } else {
        setError("The app did not start again. Run `docker compose restart jobfinder`.")
        onHold("none")
      }
      return true
    } catch (err) {
      setError(errorText(err))
      onHold("none")
      return false
    } finally {
      setBusy(false); setRestarting(false)
    }
  }

  async function signIn() {
    // Open the tab now: a browser stops a new tab that does not come directly from a click.
    const tab = window.open("about:blank", "_blank")
    setBusy(true); setError(null); setNotice(null)
    try {
      const { url: next } = await startSubscriptionLogin()
      if (tab) {
        tab.opener = null
        tab.location.href = next
      }
      setUrl(next)
      onHold("outside")
    } catch (err) {
      tab?.close()
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  async function finish(event: FormEvent) {
    event.preventDefault()
    const ok = await apply(() => finishSubscriptionLogin(code.trim()), "oauth_token")
    if (!ok) { // The server ends the sign-in after a wrong code.
      setCode(""); setUrl(null)
    }
  }

  const useSavedKey = !apiKey.trim() && Boolean(saved?.api_key) && inUse !== "api_key"
  function submitKey(event: FormEvent) {
    event.preventDefault()
    void apply(() => useSavedKey ? chooseLogin("api_key") : saveLogin(apiKey.trim()), "api_key")
  }

  const claudeAction = inUse === "oauth_token" ? "Use another account" : "Use Claude Code"
  const useSavedToken = inUse !== "oauth_token" && Boolean(saved?.oauth_token)

  return (
    <div className="flex flex-col gap-2.5">
      <div role="radiogroup" aria-label="Claude login" className="grid grid-cols-2 gap-0.5 rounded-lg bg-muted p-0.5">
        {MODES.map(({ mode: id, label }) => (
          <button key={id} type="button" role="radio" aria-checked={mode === id} disabled={busy || Boolean(url)}
            onClick={() => { setMode(id); setError(null) }}
            className={cn("flex h-7 items-center justify-center gap-1.5 rounded-md text-[13px] font-medium outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60",
              mode === id ? "bg-background text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground")}>
            {inUse === id && <span aria-hidden className="size-1.5 rounded-full bg-emerald-500" />}
            {label}
          </button>
        ))}
      </div>

      {mode === "oauth_token" && !url && (
        <Button disabled={busy} onClick={() => void (useSavedToken ? apply(() => chooseLogin("oauth_token"), "oauth_token") : signIn())}>
          {restarting ? "Restarting" : claudeAction}
        </Button>
      )}
      {mode === "oauth_token" && url && (
        <form className="flex flex-col gap-2" onSubmit={(event) => void finish(event)}>
          <div className="flex gap-1.5">
            <Input autoFocus type="password" autoComplete="off" aria-label="The code from Claude" placeholder="Paste the code" value={code} onChange={(e) => setCode(e.target.value)} />
            <Button type="submit" disabled={busy || !code.trim()}>Done</Button>
          </div>
          <a href={url} target="_blank" rel="noopener noreferrer" className="text-[12.5px] text-muted-foreground underline underline-offset-2 hover:text-foreground">Open the sign-in page</a>
        </form>
      )}
      {mode === "api_key" && (
        <form className="flex gap-1.5" onSubmit={submitKey}>
          <Input type="password" autoComplete="off" aria-label="API key" placeholder="Paste your API key" value={apiKey} onChange={(e) => setApiKey(e.target.value)} />
          <Button type="submit" className="shrink-0" disabled={busy || (!apiKey.trim() && !useSavedKey)}>
            {restarting ? "Restarting" : useSavedKey ? "Use saved key" : "Save"}
          </Button>
        </form>
      )}

      {notice && <p role="status" className="text-[12.5px] text-muted-foreground">{notice}</p>}
      {error && <p role="alert" className="text-[12.5px] text-red-700 dark:text-red-300">{error}</p>}
    </div>
  )
}
