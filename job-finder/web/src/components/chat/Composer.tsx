import { ArrowUp } from "lucide-react"
import type { ReactNode, Ref } from "react"
import { Button } from "@/components/ui/button"

type Props = {
  value: string
  onChange: (value: string) => void
  onSubmit: (text: string) => void
  busy?: boolean
  placeholder?: string
  tools?: ReactNode
  textareaRef?: Ref<HTMLTextAreaElement>
}

export function Composer({ value, onChange, onSubmit, busy = false, placeholder, tools, textareaRef }: Props) {
  const canSend = value.trim().length > 0
  const submit = () => { if (canSend) onSubmit(value) }
  return (
    <form className="flex flex-col gap-1.5 rounded-xl border bg-card p-2.5 transition-colors focus-within:border-violet-500/50"
      onSubmit={(e) => { e.preventDefault(); submit() }}>
      <textarea
        ref={textareaRef}
        aria-label="Message"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit() }
        }}
        rows={1}
        placeholder={busy ? "The agents are working. They read your message next." : (placeholder ?? "Message the agent")}
        className="field-sizing-content max-h-48 min-h-9 w-full resize-none bg-transparent px-1 leading-6 outline-none placeholder:text-muted-foreground"
      />
      <div className="flex items-center justify-between gap-1">
        <div className="min-w-0">{tools}</div>
        <Button type="submit" size="icon" className="size-7 rounded-md" disabled={!canSend} aria-label="Send"><ArrowUp /></Button>
      </div>
    </form>
  )
}
