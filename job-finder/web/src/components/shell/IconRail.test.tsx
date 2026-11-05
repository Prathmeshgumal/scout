import { render, screen } from "@testing-library/react"
import { expect, it, vi } from "vitest"
import { TooltipProvider } from "@/components/ui/tooltip"
import { IconRail } from "./IconRail"

it("gives Missions a target and Schedules a scroll", () => {
  render(
    <TooltipProvider>
      <IconRail section="goals" onSection={vi.fn()} theme="system" resolved="light" onTheme={vi.fn()} narrow={false}
        login={{ open: false, onOpenChange: vi.fn(), status: null, onChanged: vi.fn() }} onSignOut={vi.fn()} />
    </TooltipProvider>,
  )
  expect(screen.getByRole("button", { name: "Missions" }).querySelector(".lucide-target")).toBeInTheDocument()
  expect(screen.getByRole("button", { name: "Schedules" }).querySelector(".lucide-scroll-text")).toBeInTheDocument()
})
