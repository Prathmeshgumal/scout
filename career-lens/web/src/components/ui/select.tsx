import { CheckIcon, ChevronDownIcon } from "lucide-react"
import { Select as SelectPrimitive } from "radix-ui"
import { cn } from "@/lib/utils"

export type SelectChoice = { value: string; label: string }

type Props = {
  value: string
  onChange: (value: string) => void
  choices: SelectChoice[]
  /** The name for screen readers. */
  label: string
  /** The text when no choice is selected. */
  placeholder?: string
  disabled?: boolean
  className?: string
}

/** The dropdown of this app. Do not use a native select: its open list ignores the theme. */
export function Select({ value, onChange, choices, label, placeholder, disabled, className }: Props) {
  return (
    <SelectPrimitive.Root value={value || undefined} onValueChange={onChange} disabled={disabled}>
      <SelectPrimitive.Trigger aria-label={label}
        className={cn("inline-flex h-9 min-w-0 items-center justify-between gap-2 rounded-lg border bg-transparent px-2.5 text-left text-sm outline-none transition-colors hover:bg-muted/60 focus-visible:border-violet-500/60 focus-visible:ring-2 focus-visible:ring-violet-500/20 disabled:pointer-events-none disabled:opacity-50 data-[placeholder]:text-muted-foreground", className)}>
        <span className="truncate"><SelectPrimitive.Value placeholder={placeholder} /></span>
        <SelectPrimitive.Icon><ChevronDownIcon className="size-4 shrink-0 text-muted-foreground" /></SelectPrimitive.Icon>
      </SelectPrimitive.Trigger>
      <SelectPrimitive.Portal>
        <SelectPrimitive.Content position="popper" sideOffset={4} align="start"
          className="z-50 max-h-72 min-w-(--radix-select-trigger-width) overflow-hidden rounded-lg bg-popover text-popover-foreground shadow-md ring-1 ring-foreground/10 data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95">
          <SelectPrimitive.Viewport className="p-1">
            {choices.map((choice) => (
              <SelectPrimitive.Item key={choice.value} value={choice.value}
                className="relative flex cursor-default items-center rounded-md py-1.5 pr-8 pl-2 text-sm outline-none select-none data-[highlighted]:bg-muted data-[state=checked]:font-medium">
                <SelectPrimitive.ItemText>{choice.label}</SelectPrimitive.ItemText>
                <SelectPrimitive.ItemIndicator className="absolute right-2"><CheckIcon className="size-4 text-violet-600 dark:text-violet-400" /></SelectPrimitive.ItemIndicator>
              </SelectPrimitive.Item>
            ))}
          </SelectPrimitive.Viewport>
        </SelectPrimitive.Content>
      </SelectPrimitive.Portal>
    </SelectPrimitive.Root>
  )
}
