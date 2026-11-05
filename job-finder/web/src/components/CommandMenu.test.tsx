import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"
import { CommandMenu } from "./CommandMenu"

const actions = () => ({ onOpenChange: vi.fn(), onNew: vi.fn(), onJobs: vi.fn(), onPipeline: vi.fn(), onSettings: vi.fn(), onGoal: vi.fn(), onMandate: vi.fn(), onToggleTheme: vi.fn() })

describe("command menu", () => {
  it("opens and goes to the Job pipeline", async () => {
    const a = actions()
    render(<CommandMenu open goals={[]} mandates={[]} dark={false} {...a} />)
    await userEvent.click(await screen.findByRole("option", { name: "Job pipeline" }))
    expect(a.onPipeline).toHaveBeenCalledOnce()
    expect(a.onOpenChange).toHaveBeenCalledWith(false)
  })

  it("finds an item from the typed text", async () => {
    render(<CommandMenu open goals={[]} mandates={[]} dark={false} {...actions()} />)
    await userEvent.type(await screen.findByRole("combobox"), "pipeline")
    expect(screen.getByRole("option", { name: "Job pipeline" })).toBeInTheDocument()
    expect(screen.queryByRole("option", { name: "New goal" })).not.toBeInTheDocument()
  })
})
