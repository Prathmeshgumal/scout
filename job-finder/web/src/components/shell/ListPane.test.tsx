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
  it("opens the chat of a schedule with its row", async () => {
    const props = show()
    await userEvent.click(screen.getByRole("button", { name: /^(?!Delete|Hide|Show).*Daily roles/ }))
    expect(props.onOpen).toHaveBeenCalledWith("m1")
  })

  it("says when the next mission starts, and what the schedule does now", () => {
    show({ goals: [] })
    expect(screen.getByText(/^Next mission /)).toBeInTheDocument()
  })

  it("says that the schedule works while one of its missions works", () => {
    show({ goals: [run(1, { status: "running" })] })
    expect(screen.getByText("Working now")).toBeInTheDocument()
    expect(screen.queryByText(/^Next mission /)).not.toBeInTheDocument()
  })

  it("keeps the state of a paused schedule", () => {
    show({ mandates: [mandate({ status: "paused" })], goals: [run(1, { status: "running" })] })
    expect(screen.getByText("Paused")).toBeInTheDocument()
  })

  it("has no missions button with no missions", () => {
    show()
    expect(screen.queryByRole("button", { name: /Show the missions/ })).not.toBeInTheDocument()
  })

  it("opens the missions of the schedule that holds the open chat, newest first", () => {
    show({ goals: [run(1), run(2)], activeGoalId: "home" })
    const titles = screen.getAllByRole("button", { name: /^(?!Delete).*Run title/ }).map((b) => b.textContent)
    expect(titles[0]).toContain("Run title 2")
    expect(titles[1]).toContain("Run title 1")
  })

  it("marks the row of the home chat and of an open mission", () => {
    show({ goals: [run(1)], activeGoalId: "run1" })
    expect(screen.getByRole("button", { name: /^(?!Delete).*Run title 1/ })).toHaveAttribute("aria-current", "true")
    expect(screen.getByRole("button", { name: /^(?!Delete|Hide|Show).*Daily roles/ })).not.toHaveAttribute("aria-current")
  })

  it("opens a mission with no tab choice: the app shows its chat", async () => {
    const props = show({ goals: [run(1)], activeGoalId: "home" })
    await userEvent.click(screen.getByRole("button", { name: /^(?!Delete).*Run title 1/ }))
    expect(props.onOpenGoal).toHaveBeenCalledExactlyOnceWith("run1")
  })

  it("names a mission with no title by its number", () => {
    show({ goals: [run(1), run(2, { title: "" })], activeGoalId: "home" })
    expect(screen.getByRole("button", { name: /^(?!Delete).*Mission 2/ })).toBeInTheDocument()
  })

  it("shows the 5 newest missions, and a button for the older missions", async () => {
    show({ goals: [1, 2, 3, 4, 5, 6, 7].map((n) => run(n)), activeGoalId: "home" })
    expect(screen.getAllByRole("button", { name: /^(?!Delete).*Run title/ })).toHaveLength(5)
    expect(screen.queryByRole("button", { name: /^(?!Delete).*Run title 1/ })).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "Show 2 older" }))
    expect(screen.getAllByRole("button", { name: /^(?!Delete).*Run title/ })).toHaveLength(7)
    expect(screen.getByRole("button", { name: "Show fewer" })).toBeInTheDocument()
  })

  it("deletes one mission of a schedule", async () => {
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

  it("names the list and its button with the word mission", () => {
    list([goal])
    expect(screen.getByRole("region", { name: "Missions" })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "New mission" })).toBeInTheDocument()
    expect(screen.getByRole("textbox", { name: "Search missions" })).toBeInTheDocument()
  })

  it("shows only the missions that no schedule started", () => {
    list([goal, run(2)])
    expect(screen.getAllByRole("button", { name: /^(?!Delete).*Run title/ })).toHaveLength(1)
  })

  it("says that there are no missions", () => {
    list([])
    expect(screen.getByText("No missions yet. Start one to find roles.")).toBeInTheDocument()
  })
})

describe("the icons of the lists", () => {
  const icon = (name: string) => document.querySelector(`.lucide-${name}`)
  const goals = (status: GoalSummary["status"]) => render(<GoalList goals={[run(1, { mandate_id: null, status })]} activeId={null} full={false} onOpen={vi.fn()} onNew={vi.fn()} onDelete={vi.fn()} />)

  it("gives a mission a target, and a new mission a pen", () => {
    goals("done")
    expect(icon("target")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "New mission" }).querySelector(".lucide-square-pen")).toBeInTheDocument()
  })

  it.each([["running", "Working"], ["waiting", "Waiting on scrapes"], ["failed", "Failed"], ["capped", "Reached its limit"]] as const)("shows that a mission is %s", (status, label) => {
    goals(status)
    expect(screen.getByRole("img", { name: label })).toBeInTheDocument()
  })

  it("shows no status icon for a finished mission", () => {
    goals("done")
    expect(screen.queryByRole("img")).not.toBeInTheDocument()
  })

  it("gives a schedule a scroll", () => {
    show()
    expect(icon("scroll-text")).toBeInTheDocument()
  })

  it("shows a paused schedule and an ended schedule", () => {
    show({ mandates: [mandate({ status: "paused" }), mandate({ id: "m2", status: "ended", title: "Old" })] })
    expect(screen.getByRole("img", { name: "Paused" })).toBeInTheDocument()
    expect(screen.getByRole("img", { name: "Ended" })).toBeInTheDocument()
  })

  it("keeps the scroll and adds a green dot while a mission of the schedule works", () => {
    show({ goals: [run(1, { status: "running" })] })
    expect(screen.getByRole("img", { name: "Working now" })).toBeInTheDocument()
    expect(icon("scroll-text")).toBeInTheDocument()
  })

  it("gives a mission of a schedule a target and a status icon", () => {
    show({ goals: [run(1, { status: "running" })], activeGoalId: "home" })
    expect(icon("target")).toBeInTheDocument()
    expect(screen.getByRole("img", { name: "Working" })).toBeInTheDocument()
  })
})
