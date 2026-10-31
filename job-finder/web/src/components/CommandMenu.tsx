import { Briefcase, Moon, ScrollText, SquarePen, Sun, Target, Funnel, Settings as SettingsIcon } from "lucide-react"
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
  onSettings: () => void
  onGoal: (id: string) => void
  onMandate: (id: string) => void
  onToggleTheme: () => void
}

export function CommandMenu({ open, onOpenChange, goals, mandates, dark, onNew, onJobs, onPipeline, onSettings, onGoal, onMandate, onToggleTheme }: Props) {
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
    <CommandDialog open={open} onOpenChange={onOpenChange} title="Command menu" description="Go to a goal, a mandate or the roles">
      <CommandInput placeholder="Go to" />
      <CommandList>
        <CommandEmpty>Nothing found.</CommandEmpty>
        <CommandGroup heading="Actions">
          <CommandItem onSelect={run(onNew)}><SquarePen /> New goal</CommandItem>
          <CommandItem onSelect={run(onJobs)}><Briefcase /> Jobs</CommandItem>
          <CommandItem onSelect={run(onPipeline)}><Funnel /> Job pipeline</CommandItem>
          <CommandItem onSelect={run(onSettings)}><SettingsIcon /> Settings</CommandItem>
          <CommandItem onSelect={run(onToggleTheme)}>{dark ? <Sun /> : <Moon />} Use the {dark ? "light" : "dark"} theme</CommandItem>
        </CommandGroup>
        {goals.length > 0 && (
          <CommandGroup heading="Goals">
            {goals.filter((g) => !g.home_of).map((g) => <CommandItem key={g.id} value={`${g.title} ${g.id}`} onSelect={run(() => onGoal(g.id))}><Target /> {g.title}</CommandItem>)}
          </CommandGroup>
        )}
        {mandates.length > 0 && (
          <CommandGroup heading="Mandates">
            {mandates.map((m) => <CommandItem key={m.id} value={`${m.title} ${m.id}`} onSelect={run(() => onMandate(m.id))}><ScrollText /> {m.title}</CommandItem>)}
          </CommandGroup>
        )}
      </CommandList>
    </CommandDialog>
  )
}
