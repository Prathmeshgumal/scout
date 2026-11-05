import { useState } from "react"
import { Field, Problem, fieldClass } from "@/components/Status"
import { Button } from "@/components/ui/button"
import { signIn } from "@/lib/api"

export function Login({ onDone }: { onDone: () => void }) {
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
      setError(err instanceof Error ? err.message : "Sign-in failed.")
    }
    setBusy(false)
  }

  return (
    <main className="grid min-h-dvh place-items-center bg-background px-4">
      <form onSubmit={submit} className="flex w-full max-w-xs flex-col gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">career-lens console</h1>
          <p className="mt-1 text-sm text-muted-foreground">Sign in with the operator password.</p>
        </div>
        <Problem text={error} />
        <Field label="Password">
          <input type="password" autoFocus required autoComplete="current-password" value={password}
            onChange={(e) => setPassword(e.target.value)} className={fieldClass} />
        </Field>
        <Button type="submit" disabled={busy || !password}>Sign in</Button>
      </form>
    </main>
  )
}
