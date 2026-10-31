import { Briefcase, CalendarClock, Funnel, Laptop, MessagesSquare, Moon, Sun } from "lucide-react"
import type { ComponentProps } from "react"
import { LoginPopover } from "@/components/shell/LoginPopover"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import type { Theme } from "@/hooks/useTheme"
import type { Section } from "@/lib/route"
import { cn } from "@/lib/utils"

const SECTIONS: { id: Section; label: string; icon: typeof Briefcase }[] = [
  { id: "goals", label: "Goals", icon: MessagesSquare },
  { id: "mandates", label: "Mandates", icon: CalendarClock },
  { id: "jobs", label: "Jobs", icon: Briefcase },
  { id: "pipeline", label: "Job pipeline", icon: Funnel },
]

const item = "grid size-[34px] place-items-center rounded-[9px] outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring [&_svg]:size-[17px]"
const idle = "text-muted-foreground hover:bg-muted hover:text-foreground"

type Props = { section: Section; onSection: (section: Section) => void; theme: Theme; resolved: "light" | "dark"; onTheme: (theme: Theme) => void; narrow: boolean; login: ComponentProps<typeof LoginPopover> }

export function IconRail({ section, onSection, theme, resolved, onTheme, narrow, login }: Props) {
  const ThemeIcon = theme === "system" ? Laptop : resolved === "dark" ? Moon : Sun
  return (
    <nav aria-label="Main" className={cn("flex shrink-0 items-center", narrow ? "h-12 justify-around border-t px-2" : "w-[52px] flex-col gap-1.5 border-r py-2.5")}>
      {SECTIONS.map(({ id, label, icon: Icon }) => (
        <Tooltip key={id}>
          <TooltipTrigger asChild>
            <button type="button" aria-label={label} aria-current={section === id ? "page" : undefined} onClick={() => onSection(id)}
              className={cn(item, section === id ? "bg-violet-500/15 text-violet-700 ring-1 ring-violet-500/30 ring-inset dark:text-violet-300" : idle)}>
              <Icon />
            </button>
          </TooltipTrigger>
          <TooltipContent side={narrow ? "top" : "right"}>{label}</TooltipContent>
        </Tooltip>
      ))}
      {!narrow && <div className="flex-1" />}
      <DropdownMenu>
        <DropdownMenuTrigger aria-label="Theme" className={cn(item, idle)}><ThemeIcon /></DropdownMenuTrigger>
        <DropdownMenuContent side={narrow ? "top" : "right"} align="end">
          <DropdownMenuItem onSelect={() => onTheme("light")}>Light</DropdownMenuItem>
          <DropdownMenuItem onSelect={() => onTheme("dark")}>Dark</DropdownMenuItem>
          <DropdownMenuItem onSelect={() => onTheme("system")}>System</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <LoginPopover {...login} className={cn(item, idle)} side={narrow ? "top" : "right"} />
    </nav>
  )
}
