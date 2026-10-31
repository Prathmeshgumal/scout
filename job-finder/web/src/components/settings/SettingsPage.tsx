import { ExternalLink } from "lucide-react"
import { useState, type ReactNode } from "react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  finishSubscriptionLogin, getStatus, removeLogin, saveLogin, startSubscriptionLogin,
  type LoginMode, type LoginResult, type Status,
} from "@/lib/api"
import { cn } from "@/lib/utils"

const RESTART_LIMIT_MS = 60_000
const NO_RESTART = "The login is saved. Restart `job-finder serve` to use it."

type Props = { status: Status | null; onChanged: () => void; restartPollMs?: number }

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

const STATE_CLASS: Record<string, string> = {
  "In use": "bg-emerald-500/15 text-emerald-800 dark:text-emerald-300",
  "Saved, not in use": "bg-amber-500/15 text-amber-800 dark:text-amber-300",
  "Not saved": "bg-muted text-muted-foreground",
}

function LoginSection({ title, state, detail, children }: { title: string; state: string; detail?: string | null; children: ReactNode }) {
  return (
    <section aria-label={title} className="flex flex-col gap-3 rounded-xl border bg-card p-4">
      <header className="flex flex-wrap items-center gap-2">
        <h3 className="font-semibold">{title}</h3>
        <span className={cn("rounded-md px-1.5 py-0.5 text-[12px] font-medium", STATE_CLASS[state])}>{state}</span>
        {detail && <span className="text-[12.5px] text-muted-foreground">{detail}</span>}
      </header>
      {children}
    </section>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return <label className="flex flex-col gap-1"><span className="font-medium">{label}</span>{children}</label>
}

export function SettingsPage({ status, onChanged, restartPollMs = 1000 }: Props) {
  const [url, setUrl] = useState<string | null>(null)
  const [code, setCode] = useState("")
  const [token, setToken] = useState("")
  const [apiKey, setApiKey] = useState("")
  const [subscriptionLabel, setSubscriptionLabel] = useState("")
  const [keyLabel, setKeyLabel] = useState("")
  const [busy, setBusy] = useState(false)
  const [restarting, setRestarting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [confirming, setConfirming] = useState<LoginMode | null>(null)

  const account = status?.claude_account
  const saved = account?.saved
  const usesKey = status?.claude_login === "API key"
  const usesToken = status?.claude_login === "Claude subscription token"
  const stateOf = (mode: LoginMode, inUse: boolean) => inUse ? "In use" : saved?.[mode] ? "Saved, not in use" : "Not saved"
  const when = account?.saved_at ? `saved ${new Date(account.saved_at).toLocaleDateString(undefined, { dateStyle: "medium" })}` : null
  const detail = [account?.account_label, when].filter(Boolean).join(", ")

  async function run(action: () => Promise<LoginResult>, done: string) {
    setBusy(true); setError(null); setNotice(null)
    try {
      const result = await action()
      // Do not keep a credential in the page after the server has it.
      setCode(""); setToken(""); setApiKey(""); setSubscriptionLabel(""); setKeyLabel(""); setUrl(null); setConfirming(null)
      if (!result.restarting) {
        setNotice(NO_RESTART)
        onChanged()
        return
      }
      setRestarting(true)
      if (await waitForRestart(account?.loaded_at, restartPollMs)) {
        toast.success(done)
        onChanged()
      } else {
        setError("The app did not start again. Run `docker compose restart jobfinder`.")
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "The change failed.")
    } finally {
      setBusy(false); setRestarting(false)
    }
  }

  async function startSignIn() {
    setBusy(true); setError(null); setNotice(null)
    try {
      setUrl((await startSubscriptionLogin()).url)
    } catch (err) {
      setError(err instanceof Error ? err.message : "The sign-in did not start.")
    } finally {
      setBusy(false)
    }
  }

  const removeButton = (mode: LoginMode, name: string) => saved?.[mode] && (
    confirming === mode
      ? <Button variant="destructive" size="sm" disabled={busy} className="self-start" onClick={() => void run(() => removeLogin(mode), `Removed the ${name}.`)}>Yes, remove the {name}</Button>
      : <Button variant="ghost" size="sm" disabled={busy} className="self-start" onClick={() => setConfirming(mode)}>Remove the {name}</Button>
  )

  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex max-w-[680px] flex-col gap-4 p-4">
        <header><h1 className="text-[17px] font-semibold">Settings</h1></header>
        <div className="flex flex-col gap-1">
          <h2 className="font-semibold">Claude login</h2>
          <p className="text-muted-foreground">The agents use this login. Only this computer can change it. The app restarts after a change, and running goals continue after the restart.</p>
        </div>

        {account?.restart_required && !restarting && (
          <p className="rounded-lg bg-amber-500/10 px-2.5 py-2 text-amber-800 dark:text-amber-200">
            The saved login is not in use. Restart the app. If this message stays, a value in the environment wins over the saved login.
          </p>
        )}
        {restarting && <p role="status" className="rounded-lg bg-violet-500/10 px-2.5 py-2">The app restarts to use the new login.</p>}
        {notice && <p role="status" className="rounded-lg bg-muted px-2.5 py-2">{notice}</p>}
        {error && <p role="alert" className="rounded-lg bg-red-500/10 px-2.5 py-2 text-red-800 dark:text-red-200">{error}</p>}

        <LoginSection title="Claude subscription" state={stateOf("oauth_token", usesToken)} detail={usesToken ? detail : null}>
          {usesKey && <p className="text-muted-foreground">The API key wins over the subscription. Remove the API key to use the subscription.</p>}
          {saved?.oauth_token && <p className="text-muted-foreground">To use another account, first sign in to that account on claude.ai in your browser. Then sign in here. The new login replaces the old login.</p>}
          {!url ? (
            <Button disabled={busy} className="self-start" onClick={() => void startSignIn()}>
              {saved?.oauth_token ? "Sign in with another account" : "Sign in with Claude"}
            </Button>
          ) : (
            <form className="flex flex-col gap-2" onSubmit={(e) => { e.preventDefault(); void run(() => finishSubscriptionLogin(code.trim(), subscriptionLabel.trim()), "Signed in with your Claude subscription.") }}>
              <a href={url} target="_blank" rel="noopener noreferrer" className="flex items-center gap-1 self-start font-medium text-violet-700 hover:underline dark:text-violet-300">
                Open the Claude sign-in page <ExternalLink className="size-3.5" aria-hidden />
              </a>
              <p className="text-muted-foreground">Approve the login. Then copy the code from that page and paste it here.</p>
              <Field label="Code from Claude"><Input type="password" autoComplete="off" value={code} onChange={(e) => setCode(e.target.value)} /></Field>
              <Field label="Account label (optional)"><Input maxLength={120} autoComplete="off" placeholder="For example, Work" value={subscriptionLabel} onChange={(e) => setSubscriptionLabel(e.target.value)} /></Field>
              <Button type="submit" className="self-start" disabled={busy || !code.trim()}>Finish the sign-in</Button>
            </form>
          )}
          <form className="flex flex-col gap-2 border-t pt-3" onSubmit={(e) => { e.preventDefault(); void run(() => saveLogin(token.trim()), "Saved the subscription token.") }}>
            <Field label="Token from claude setup-token"><Input type="password" autoComplete="off" placeholder="sk-ant-oat..." value={token} onChange={(e) => setToken(e.target.value)} /></Field>
            <Button type="submit" variant="outline" size="sm" className="self-start" disabled={busy || !token.trim()}>Save the token</Button>
          </form>
          {removeButton("oauth_token", "subscription token")}
        </LoginSection>

        <LoginSection title="Anthropic API key" state={stateOf("api_key", usesKey)} detail={usesKey ? detail : null}>
          <p className="text-muted-foreground">An API key wins over a subscription. The Anthropic API bills each request to the key.</p>
          <form className="flex flex-col gap-2" onSubmit={(e) => { e.preventDefault(); void run(() => saveLogin(apiKey.trim(), keyLabel.trim()), "Saved the API key.") }}>
            <Field label="API key"><Input type="password" autoComplete="off" placeholder="sk-ant-..." value={apiKey} onChange={(e) => setApiKey(e.target.value)} /></Field>
            <Field label="Account label (optional)"><Input maxLength={120} autoComplete="off" value={keyLabel} onChange={(e) => setKeyLabel(e.target.value)} /></Field>
            <Button type="submit" className="self-start" disabled={busy || !apiKey.trim()}>Save the API key</Button>
          </form>
          {removeButton("api_key", "API key")}
        </LoginSection>
      </div>
    </div>
  )
}
