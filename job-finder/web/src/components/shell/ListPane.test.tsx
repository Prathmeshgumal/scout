import { render, screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"
import type { GoalSummary, Mandate } from "@/lib/api"
import { GoalList, MandateList } from "./ListPane"

const mandate = (over: Partial<Mandate> = {}) => ({ id: "m1", home_goal_id: "home", title: "Daily roles", status: "active", next_run_at: "2026-11-06T09:00:00Z", ...over }) as Mandate
const run = (n: number, over: Partial<GoalSummary> = {}) => ({
  id: `run${n}`, title: `Run title ${n}`, status: "done", mandate_id: "m1", home_of: null, jobs: 2, fit: 1,
  created_at: `2026-11-0${n}T09:00:00Z`, ...over,
}) as GoalSummary

function show(over: Partial<Parameters<typeof MandateList>[0]> = {}) {
  const props = { mandates: [mandate()], goals: [] as GoalSummary[], activeGoalId: null, full: false, onOpen: vi.fn(), onOpenGoal: vi.fn(), onNew: vi.fn(), onDelete: vi.fn(), onDeleteGoal: vi.fn(), ...over }
  render(<MandateList {...props} />)
  return props
}

describe("MandateList", () => {
  it("opens the chat of a mandate with its row", async () => {
    const props = show()
    await userEvent.click(screen.getByRole("button", { name: /^(?!Delete|Hide|Show).*Daily roles/ }))
    expect(props.onOpen).toHaveBeenCalledWith("m1")
  })

  it("says when the next goal starts, and what the mandate does now", () => {
    show({ goals: [] })
    expect(screen.getByText(/^Next goal /)).toBeInTheDocument()
  })

  it("says that the mandate works while one of its goals works", () => {
    show({ goals: [run(1, { status: "running" })] })
    expect(screen.getByText("Working now")).toBeInTheDocument()
    expect(screen.queryByText(/^Next goal /)).not.toBeInTheDocument()
  })

  it("keeps the state of a paused mandate", () => {
    show({ mandates: [mandate({ status: "paused" })], goals: [run(1, { status: "running" })] })
    expect(screen.getByText("Paused")).toBeInTheDocument()
  })

  it("has no goals button with no goals", () => {
    show()
    expect(screen.queryByRole("button", { name: /Show the goals/ })).not.toBeInTheDocument()
  })

  it("opens the goals of the mandate that holds the open chat, newest first", () => {
    show({ goals: [run(1), run(2)], activeGoalId: "home" })
    const titles = screen.getAllByRole("button", { name: /^(?!Delete).*Run title/ }).map((b) => b.textContent)
    expect(titles[0]).toContain("Run title 2")
    expect(titles[1]).toContain("Run title 1")
  })

  it("marks the row of the home chat and of an open goal", () => {
    show({ goals: [run(1)], activeGoalId: "run1" })
    expect(screen.getByRole("button", { name: /^(?!Delete).*Run title 1/ })).toHaveAttribute("aria-current", "true")
    expect(screen.getByRole("button", { name: /^(?!Delete|Hide|Show).*Daily roles/ })).not.toHaveAttribute("aria-current")
  })

  it("opens a goal with no tab choice: the app shows its chat", async () => {
    const props = show({ goals: [run(1)], activeGoalId: "home" })
    await userEvent.click(screen.getByRole("button", { name: /^(?!Delete).*Run title 1/ }))
    expect(props.onOpenGoal).toHaveBeenCalledExactlyOnceWith("run1")
  })

  it("names a goal with no title by its number", () => {
    show({ goals: [run(1), run(2, { title: "" })], activeGoalId: "home" })
    expect(screen.getByRole("button", { name: /^(?!Delete).*Goal 2/ })).toBeInTheDocument()
  })

  it("shows the 5 newest goals, and a button for the older goals", async () => {
    show({ goals: [1, 2, 3, 4, 5, 6, 7].map((n) => run(n)), activeGoalId: "home" })
    expect(screen.getAllByRole("button", { name: /^(?!Delete).*Run title/ })).toHaveLength(5)
    expect(screen.queryByRole("button", { name: /^(?!Delete).*Run title 1/ })).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "Show 2 older" }))
    expect(screen.getAllByRole("button", { name: /^(?!Delete).*Run title/ })).toHaveLength(7)
    expect(screen.getByRole("button", { name: "Show fewer" })).toBeInTheDocument()
  })

  it("deletes one goal of a mandate", async () => {
    const goal = run(1)
    const props = show({ goals: [goal], activeGoalId: "home" })
    await userEvent.click(screen.getByRole("button", { name: "Delete Run title 1" }))
    expect(props.onDeleteGoal).toHaveBeenCalledWith(goal)
    expect(within(document.body).getByRole("button", { name: "Delete Daily roles" })).toBeInTheDocument()
  })
})

describe("GoalList", () => {
  const goal = run(1, { mandate_id: null })
  const list = (goals: GoalSummary[]) => render(<GoalList goals={goals} activeId={null} full={false} onOpen={vi.fn()} onNew={vi.fn()} onDelete={vi.fn()} />)

  it("names the list and its button with the word goal", () => {
    list([goal])
    expect(screen.getByRole("region", { name: "Goals" })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "New goal" })).toBeInTheDocument()
    expect(screen.getByRole("textbox", { name: "Search goals" })).toBeInTheDocument()
  })

  it("shows only the goals that no mandate started", () => {
    list([goal, run(2)])
    expect(screen.getAllByRole("button", { name: /^(?!Delete).*Run title/ })).toHaveLength(1)
  })

  it("says that there are no goals", () => {
    list([])
    expect(screen.getByText("No goals yet. Start one to find roles.")).toBeInTheDocument()
  })
})

describe("the icons of the lists", () => {
  const icon = (name: string) => document.querySelector(`.lucide-${name}`)
  const goals = (status: GoalSummary["status"]) => render(<GoalList goals={[run(1, { mandate_id: null, status })]} activeId={null} full={false} onOpen={vi.fn()} onNew={vi.fn()} onDelete={vi.fn()} />)

  it("gives a goal a target, and a new goal a pen", () => {
    goals("done")
    expect(icon("target")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "New goal" }).querySelector(".lucide-square-pen")).toBeInTheDocument()
  })

  it.each([["running", "Working"], ["waiting", "Waiting on scrapes"], ["failed", "Failed"], ["capped", "Reached its limit"]] as const)("shows that a goal is %s", (status, label) => {
    goals(status)
    expect(screen.getByRole("img", { name: label })).toBeInTheDocument()
  })

  it("shows no status icon for a finished goal", () => {
    goals("done")
    expect(screen.queryByRole("img")).not.toBeInTheDocument()
  })

  it("gives a mandate a scroll", () => {
    show()
    expect(icon("scroll-text")).toBeInTheDocument()
  })

  it("shows a paused mandate and an ended mandate", () => {
    show({ mandates: [mandate({ status: "paused" }), mandate({ id: "m2", status: "ended", title: "Old" })] })
    expect(screen.getByRole("img", { name: "Paused" })).toBeInTheDocument()
    expect(screen.getByRole("img", { name: "Ended" })).toBeInTheDocument()
  })

  it("keeps the scroll and adds a green dot while a goal of the mandate works", () => {
    show({ goals: [run(1, { status: "running" })] })
    expect(screen.getByRole("img", { name: "Working now" })).toBeInTheDocument()
    expect(icon("scroll-text")).toBeInTheDocument()
  })

  it("gives a goal of a mandate a target and a status icon", () => {
    show({ goals: [run(1, { status: "running" })], activeGoalId: "home" })
    expect(icon("target")).toBeInTheDocument()
    expect(screen.getByRole("img", { name: "Working" })).toBeInTheDocument()
  })
})
