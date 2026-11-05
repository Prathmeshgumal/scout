import { Loader2, RotateCcw, Square, Info } from "lucide-react"
import { useEffect, useRef, type ReactNode } from "react"
import { Button } from "@/components/ui/button"
import type { GoalStatus, TimeLimitCheck } from "@/lib/api"
import { type ChatMessage, type GoalView, type Part } from "@/lib/chat-state"
import { splitChoices, type Choice } from "@/lib/choices"
import { canRetry, canStop } from "@/lib/goals"
import { AgentCard } from "./AgentCard"
import { Markdown } from "./Markdown"

function PartView({ part, status, onActivity }: { part: Part; status: GoalStatus; onActivity?: (stepId: string | null) => void }) {
  switch (part.kind) {
    case "text":
      return <div className="leading-relaxed break-words"><Markdown>{splitChoices(part.text).text}</Markdown></div>
    case "agent":
      return <AgentCard line={part} status={status} onActivity={onActivity} />
    case "error":
      return <p className="rounded-lg bg-red-500/10 px-3 py-2 break-words text-red-800 dark:text-red-200">{part.text}</p>
    case "notice":
      return <p className="flex items-start gap-1.5 text-[12px] text-muted-foreground"><Info className="mt-[1px] size-3.5 shrink-0" aria-hidden /> <span className="min-w-0 break-words">{part.text}</span></p>
  }
}

function Message({ message, status, onChoose, onActivity }: { message: ChatMessage; status: GoalStatus; onChoose?: (text: string) => void; onActivity?: (stepId: string | null) => void }) {
  if (message.role === "user") {
    const text = message.parts[0]?.kind === "text" ? message.parts[0].text : ""
    return <div className="ml-auto max-w-[85%] rounded-xl bg-muted px-3 py-2 leading-relaxed break-words whitespace-pre-wrap">{text}</div>
  }
  const lastText = [...message.parts].reverse().find((p) => p.kind === "text")
  const choices: Choice[] = lastText?.kind === "text" ? splitChoices(lastText.text).choices : []
  return (
    <div className="flex flex-col gap-2">
      {message.parts.map((part, i) => <PartView key={i} part={part} status={status} onActivity={onActivity} />)}
      {onChoose && choices.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {choices.map((c) => (
            <Button key={c.label} size="sm" variant={c.danger ? "destructive" : "outline"} onClick={() => onChoose(c.label)}>{c.label}</Button>
          ))}
        </div>
      )}
    </div>
  )
}

type Props = {
  view: GoalView | null
  reason: string | null
  thinking: boolean
  readOnly: boolean
  timeLimit: TimeLimitCheck | null
  onChoose: (text: string) => void
  onStop: () => void
  onRetry: () => void
  /** Opens a step in the Activity tab. A chat without that tab does not give it. */
  onActivity?: (stepId: string | null) => void
  onContinue: () => void
  composer: ReactNode
}

export function ChatView({ view, reason, thinking, readOnly, timeLimit, onChoose, onStop, onRetry, onContinue, onActivity, composer }: Props) {
  const scroller = useRef<HTMLDivElement>(null)
  const stuck = useRef(true)
  const messages = view?.messages
  useEffect(() => {
    const box = scroller.current
    if (box && stuck.current) box.scrollTop = box.scrollHeight
  }, [messages, thinking])
  const status = view?.status
  const last = messages?.at(-1)
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div ref={scroller} className="min-h-0 flex-1 overflow-y-auto"
        onScroll={(e) => { const b = e.currentTarget; stuck.current = b.scrollHeight - b.scrollTop - b.clientHeight < 48 }}>
        <div className="mx-auto flex w-full max-w-[720px] flex-col gap-5 px-4 py-5">
          {!view && <div className="flex flex-col gap-2" role="status" aria-label="Loading the chat"><div className="skeleton h-3.5 w-3/4" /><div className="skeleton h-3.5 w-1/2" /></div>}
          {view && messages?.map((message) => (
            <Message key={message.id} message={message} status={view.status} onActivity={onActivity}
              onChoose={message === last && message.closed && !thinking && !readOnly ? onChoose : undefined} />
          ))}
          {thinking && <p className="flex items-center gap-2 text-[12px] text-muted-foreground"><Loader2 className="size-3.5 animate-spin text-violet-500" aria-hidden /> Working</p>}
        </div>
      </div>
      <div className="mx-auto flex w-full max-w-[720px] flex-col gap-2 px-4 pb-3">
        {(canStop(status) || canRetry(status) || timeLimit?.ok) && (
          <div className="flex min-w-0 items-center justify-between gap-3 text-[12px] text-muted-foreground">
            <span className="min-w-0 truncate" title={reason ?? undefined}>{status === "waiting" ? (reason ?? "Waiting for LinkedIn") : canRetry(status) ? reason : ""}</span>
            <span className="flex shrink-0 gap-1.5">
              {canStop(status) && <Button size="sm" variant="outline" onClick={onStop}><Square aria-hidden /> Stop</Button>}
              {canRetry(status) && <Button size="sm" variant="outline" onClick={onRetry}><RotateCcw aria-hidden /> Retry</Button>}
              {timeLimit?.ok && <Button size="sm" variant="outline" onClick={onContinue}>Continue</Button>}
            </span>
          </div>
        )}
        {readOnly
          ? <p className="rounded-xl border bg-card p-3 text-muted-foreground">This is a test goal of one subagent. Continue it in the terminal with job-finder agent.</p>
          : composer}
      </div>
    </div>
  )
}
