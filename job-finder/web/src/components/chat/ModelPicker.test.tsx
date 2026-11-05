import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"
import { TooltipProvider } from "@/components/ui/tooltip"
import { ModelPicker } from "./ModelPicker"

const ALL = ["low", "medium", "high", "xhigh", "max"]
const models = [
  { id: "claude-fable-5-1", label: "Fable 5.1", detail: "For the hardest research", efforts: ALL, badge: "Requires usage credits" },
  { id: "claude-opus-5-5", label: "Opus 5.5", detail: "For complex missions", efforts: ALL },
  { id: "claude-sonnet-5-5", label: "Sonnet 5.5", detail: "Good for most missions", efforts: ALL },
  { id: "claude-haiku-4-5", label: "Haiku 4.5", detail: "Fastest for quick answers", efforts: [] },
  { id: "claude-sonnet-4-6", label: "Sonnet 4.6", detail: "Earlier Sonnet", efforts: ["low", "medium", "high", "max"] },
]
const efforts = ALL.map((id, i) => ({ id, label: ["Low", "Medium", "High", "Extra", "Max"][i] }))

function show(effort = "medium", note?: string, model = "claude-sonnet-5-5", locked?: string) {
  const onChange = vi.fn()
  render(<TooltipProvider><ModelPicker catalog={{ models, efforts, default: { model, effort } }} choice={{ model, effort }} onChange={(c) => onChange(c.model, c.effort)} note={note} locked={locked} /></TooltipProvider>)
  return onChange
}

describe("ModelPicker", () => {
  it("shows the model and the effort under the message box, as Claude does", () => {
    show()
    expect(screen.getByRole("button", { name: "Model: Sonnet 5.5" })).toHaveTextContent("Sonnet 5.5")
    expect(screen.getByRole("button", { name: "Effort: Medium" })).toHaveTextContent("Medium")
  })

  it("changes the model from its menu, and keeps the effort", async () => {
    const onChange = show("high", "New missions of this schedule use this model.")
    await userEvent.click(screen.getByRole("button", { name: "Model: Sonnet 5.5" }))
    expect(screen.getByRole("menuitemradio", { name: /Sonnet 5.5/ })).toHaveAttribute("aria-checked", "true")
    expect(screen.getByText("New missions of this schedule use this model.")).toBeInTheDocument()
    await userEvent.click(screen.getByRole("menuitemradio", { name: /Opus 5.5/ }))
    expect(onChange).toHaveBeenCalledWith("claude-opus-5-5", "high")
  })

  it("lists the models of the SDK, with their badge", async () => {
    show()
    await userEvent.click(screen.getByRole("button", { name: "Model: Sonnet 5.5" }))
    expect(screen.getAllByRole("menuitemradio")).toHaveLength(models.length)
    expect(screen.getByRole("menuitemradio", { name: /Fable 5.1/ })).toHaveTextContent("Requires usage credits")
    expect(screen.queryByRole("menuitem", { name: "More models" })).toBeNull()
  })

  it("shows only the effort levels of the model", async () => {
    show("max", undefined, "claude-sonnet-4-6")
    await userEvent.click(screen.getByRole("button", { name: "Effort: Max" }))
    expect(screen.getByRole("slider", { name: "Effort" })).toHaveAttribute("aria-valuemax", "3")
  })

  it("moves to the next lower level when the new model does not have the effort", async () => {
    const onChange = show("xhigh")
    await userEvent.click(screen.getByRole("button", { name: "Model: Sonnet 5.5" }))
    await userEvent.click(screen.getByRole("menuitemradio", { name: /Sonnet 4.6/ }))
    expect(onChange).toHaveBeenCalledWith("claude-sonnet-4-6", "high")
  })

  it("has no effort chip for a model without effort levels", () => {
    show("medium", undefined, "claude-haiku-4-5")
    expect(screen.getByRole("button", { name: "Model: Haiku 4.5" })).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /^Effort/ })).toBeNull()
  })

  it("is locked while the agents work, and says why", async () => {
    show("medium", undefined, "claude-sonnet-5-5", "Stop the work to change the model or the effort.")
    expect(screen.getByRole("button", { name: "Model: Sonnet 5.5" })).toBeDisabled()
    expect(screen.getByRole("button", { name: "Effort: Medium" })).toBeDisabled()
    await userEvent.hover(screen.getByTestId("model-lock"))
    expect((await screen.findAllByText("Stop the work to change the model or the effort."))[0]).toBeInTheDocument()
  })

  it("moves the effort on a Faster to Smarter slider", async () => {
    const onChange = show()
    await userEvent.click(screen.getByRole("button", { name: "Effort: Medium" }))
    expect(screen.getByText("Faster")).toBeInTheDocument()
    expect(screen.getByText("Smarter")).toBeInTheDocument()
    expect(screen.getByText("Recommended")).toBeInTheDocument()
    const slider = screen.getByRole("slider", { name: "Effort" })
    expect(slider).toHaveAttribute("aria-valuetext", "Medium")
    expect(slider).toHaveFocus()
    await userEvent.keyboard("{ArrowRight}")
    expect(onChange).toHaveBeenCalledWith("claude-sonnet-5-5", "high")
  })

  it("marks the effort level, so that the track can glow more at a higher level", async () => {
    show("max")
    const chip = screen.getByRole("button", { name: "Effort: Max" })
    expect(chip).toHaveAttribute("data-level", "4")
    await userEvent.click(chip)
    expect(document.querySelector(".effort-fill")).toHaveAttribute("data-level", "4")
  })
})
