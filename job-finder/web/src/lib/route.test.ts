import { expect, it } from "vitest"
import { parseRoute, routePath } from "./route"

const ID = "4df9e024-b009-425d-8550-c20bcde9af8f"

it.each([
  ["/", { section: "chats", goalId: null, tab: "chat" }],
  [`/goals/${ID}`, { section: "chats", goalId: ID, tab: "chat" }],
  [`/goals/${ID}/jobs`, { section: "chats", goalId: ID, tab: "jobs" }],
  ["/jobs", { section: "jobs", goalId: null }],
  ["/pipeline", { section: "pipeline", goalId: null }],
  ["/settings", { section: "settings", goalId: null }],
  ["/mandates", { section: "mandates", mandateId: null }],
  [`/mandates/${ID}`, { section: "mandates", mandateId: ID }],
])("reads %s and writes the same path", (path, want) => {
  const route = parseRoute(path)
  expect(route).toMatchObject(want)
  expect(routePath(route)).toBe(path)
})

it("opens a new chat for an unknown path", () => {
  expect(routePath(parseRoute("/goals/not-an-id"))).toBe("/")
})
