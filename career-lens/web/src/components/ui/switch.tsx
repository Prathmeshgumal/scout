import { Switch as SwitchPrimitive } from "radix-ui"
import { cn } from "@/lib/utils"

type Props = { checked: boolean; onChange: (checked: boolean) => void; label?: string; disabled?: boolean; className?: string }

/** An on and off switch. Put it in a label, or give it a `label`. */
export function Switch({ checked, onChange, label, disabled, className }: Props) {
  return (
    <SwitchPrimitive.Root checked={checked} onCheckedChange={onChange} aria-label={label} disabled={disabled}
      className={cn("inline-flex h-5 w-9 shrink-0 items-center rounded-full border border-transparent bg-muted-foreground/30 p-0.5 outline-none transition-colors focus-visible:ring-2 focus-visible:ring-violet-500/40 disabled:opacity-50 data-[state=checked]:bg-violet-600", className)}>
      <SwitchPrimitive.Thumb className="block size-4 rounded-full bg-white shadow-sm transition-transform data-[state=checked]:translate-x-4" />
    </SwitchPrimitive.Root>
  )
}
