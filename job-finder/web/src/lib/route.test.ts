import { expect, it } from "vitest"
import { parseRoute, routePath } from "./route"

const ID = "4df9e024-b009-425d-8550-c20bcde9af8f"

it.each([
  ["/", { section: "goals", goalId: null, tab: "chat" }],
  [`/missions/${ID}`, { section: "goals", goalId: ID, tab: "chat" }],
  [`/missions/${ID}/jobs`, { section: "goals", goalId: ID, tab: "jobs" }],
  [`/missions/${ID}/activity`, { section: "goals", goalId: ID, tab: "activity" }],
  ["/jobs", { section: "jobs", goalId: null }],
  ["/pipeline", { section: "pipeline", goalId: null }],
  ["/schedules", { section: "mandates", mandateId: null }],
  [`/schedules/${ID}`, { section: "mandates", mandateId: ID }],
])("reads %s and writes the same path", (path, want) => {
  const route = parseRoute(path)
  expect(route).toMatchObject(want)
  expect(routePath(route)).toBe(path)
})

it("opens a new mission for an unknown path", () => {
  expect(routePath(parseRoute("/missions/not-an-id"))).toBe("/")
})

it("opens the earlier links at the new paths", () => {
  expect(routePath(parseRoute(`/goals/${ID}/activity`))).toBe(`/missions/${ID}/activity`)
  expect(routePath(parseRoute(`/mandates/${ID}`))).toBe(`/schedules/${ID}`)
  expect(routePath(parseRoute("/mandates"))).toBe("/schedules")
})
