import { ArrowUp, X } from "lucide-react"
import type { ReactNode, Ref } from "react"
import { Button } from "@/components/ui/button"
import type { FileInfo } from "@/lib/api"

type Props = {
  value: string
  onChange: (value: string) => void
  onSubmit: (text: string) => void
  busy?: boolean
  placeholder?: string
  tools?: ReactNode
  /** The files that the next message attaches. */
  attached?: FileInfo[]
  onRemove?: (id: string) => void
  /** A file is uploading: the message waits for it. */
  uploading?: boolean
  textareaRef?: Ref<HTMLTextAreaElement>
}

export function Composer({ value, onChange, onSubmit, busy = false, placeholder, tools, textareaRef, attached = [], onRemove, uploading = false }: Props) {
  const canSend = value.trim().length > 0 && !uploading
  const submit = () => { if (canSend) onSubmit(value) }
  return (
    <form className="flex flex-col gap-1.5 rounded-xl border bg-card p-2.5 transition-colors focus-within:border-violet-500/50"
      onSubmit={(e) => { e.preventDefault(); submit() }}>
      {attached.length > 0 && (
        <ul aria-label="Attached files" className="flex flex-wrap gap-1.5">
          {attached.map((file) => (
            <li key={file.id} className="flex max-w-full items-center gap-1 rounded-md bg-muted py-0.5 pr-0.5 pl-2 text-xs">
              <span className="truncate">{file.name}</span>
              {!file.readable && <span className="shrink-0 text-muted-foreground">(agents cannot read this)</span>}
              <button type="button" aria-label={`Remove ${file.name}`} onClick={() => onRemove?.(file.id)}
                className="grid size-5 shrink-0 place-items-center rounded text-muted-foreground hover:bg-background hover:text-foreground">
                <X className="size-3" />
              </button>
            </li>
          ))}
        </ul>
      )}
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
        <div className="flex min-w-0 items-center gap-1">{tools}</div>
        <Button type="submit" size="icon" className="size-7 rounded-md" disabled={!canSend} aria-label="Send"><ArrowUp /></Button>
      </div>
    </form>
  )
}
