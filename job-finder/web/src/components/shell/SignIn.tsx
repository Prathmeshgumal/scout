import { useState } from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { useTheme } from "@/hooks/useTheme"
import { signIn } from "@/lib/api"

export function SignIn({ onDone }: { onDone: () => void }) {
  useTheme()
  const [password, setPassword] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await signIn(password)
      setPassword("")
      onDone()
    } catch (err) {
      setError(err instanceof Error ? err.message : "The sign-in failed.")
    }
    setBusy(false)
  }

  return (
    <main className="grid min-h-dvh place-items-center bg-background px-4">
      <form onSubmit={submit} className="flex w-full max-w-xs flex-col gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">job-finder</h1>
          <p className="mt-1 text-sm text-muted-foreground">Sign in with the dashboard password.</p>
        </div>
        {error && <p role="alert" className="rounded-lg bg-red-500/10 px-3 py-2 text-sm text-red-700 dark:text-red-300">{error}</p>}
        <label className="flex flex-col gap-1 text-xs text-muted-foreground">
          Password
          <Input type="password" autoFocus required autoComplete="current-password" value={password}
            onChange={(e) => setPassword(e.target.value)} />
        </label>
        <Button type="submit" disabled={busy || !password}>Sign in</Button>
      </form>
    </main>
  )
}
