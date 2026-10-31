import { Briefcase, CalendarClock, MessageSquare, Moon, Plus, Sun, Funnel } from "lucide-react"
import { useEffect } from "react"
import { CommandDialog, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command"
import type { GoalSummary, Mandate } from "@/lib/api"

type Props = {
  open: boolean
  onOpenChange: (open: boolean) => void
  goals: GoalSummary[]
  mandates: Mandate[]
  dark: boolean
  onNew: () => void
  onJobs: () => void
  onPipeline: () => void
  onGoal: (id: string) => void
  onMandate: (id: string) => void
  onToggleTheme: () => void
}

export function CommandMenu({ open, onOpenChange, goals, mandates, dark, onNew, onJobs, onPipeline, onGoal, onMandate, onToggleTheme }: Props) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key.toLowerCase() === "k" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault()
        onOpenChange(!open)
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [open, onOpenChange])
  const run = (action: () => void) => () => { onOpenChange(false); action() }
  return (
    <CommandDialog open={open} onOpenChange={onOpenChange} title="Command menu" description="Go to a chat, a mandate or the roles">
      <CommandInput placeholder="Go to" />
      <CommandList>
        <CommandEmpty>Nothing found.</CommandEmpty>
        <CommandGroup heading="Actions">
          <CommandItem onSelect={run(onNew)}><Plus /> New chat</CommandItem>
          <CommandItem onSelect={run(onJobs)}><Briefcase /> Jobs</CommandItem>
          <CommandItem onSelect={run(onPipeline)}><Funnel /> Job pipeline</CommandItem>
          <CommandItem onSelect={run(onToggleTheme)}>{dark ? <Sun /> : <Moon />} Use the {dark ? "light" : "dark"} theme</CommandItem>
        </CommandGroup>
        {goals.length > 0 && (
          <CommandGroup heading="Chats">
            {goals.filter((g) => !g.home_of).map((g) => <CommandItem key={g.id} value={`${g.title} ${g.id}`} onSelect={run(() => onGoal(g.id))}><MessageSquare /> {g.title}</CommandItem>)}
          </CommandGroup>
        )}
        {mandates.length > 0 && (
          <CommandGroup heading="Mandates">
            {mandates.map((m) => <CommandItem key={m.id} value={`${m.title} ${m.id}`} onSelect={run(() => onMandate(m.id))}><CalendarClock /> {m.title}</CommandItem>)}
          </CommandGroup>
        )}
      </CommandList>
    </CommandDialog>
  )
}
