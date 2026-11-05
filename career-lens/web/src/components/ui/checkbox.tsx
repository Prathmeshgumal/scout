import { CheckIcon } from "lucide-react"
import { Checkbox as CheckboxPrimitive } from "radix-ui"
import { cn } from "@/lib/utils"

type Props = { checked: boolean; onChange: (checked: boolean) => void; label?: string; disabled?: boolean; className?: string }

/** The checkbox of this app. Put it in a label, or give it a `label`. */
export function Checkbox({ checked, onChange, label, disabled, className }: Props) {
  return (
    <CheckboxPrimitive.Root checked={checked} onCheckedChange={(value) => onChange(value === true)} aria-label={label} disabled={disabled}
      className={cn("grid size-4 shrink-0 place-items-center rounded-[5px] border outline-none transition-colors focus-visible:ring-2 focus-visible:ring-violet-500/40 disabled:opacity-50 data-[state=checked]:border-violet-600 data-[state=checked]:bg-violet-600 data-[state=checked]:text-white", className)}>
      <CheckboxPrimitive.Indicator><CheckIcon className="size-3" /></CheckboxPrimitive.Indicator>
    </CheckboxPrimitive.Root>
  )
}
