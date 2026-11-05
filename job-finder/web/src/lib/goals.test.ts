import { expect, it, vi } from "vitest"
import { onlyFor } from "./goals"

it("drops a late reply for a goal that is no longer open", () => {
  const apply = vi.fn()
  let open = "a"
  const reply = onlyFor(() => open, "a", apply)
  open = "b"
  reply("late")
  expect(apply).not.toHaveBeenCalled()
})

it("keeps a reply for the open goal", () => {
  const apply = vi.fn()
  onlyFor(() => "a", "a", apply)("now")
  expect(apply).toHaveBeenCalledWith("now")
})
