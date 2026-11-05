import { describe, expect, it } from "vitest"
import { deleteText } from "./delete"

const preview = { goals: 1, jobs: 12, notes: 1, working: false }

describe("deleteText", () => {
  it("says that a mission delete removes its jobs from the Jobs page and keeps the pipeline", () => {
    expect(deleteText("goal", preview)).toBe(
      "This deletes the mission, its chat, 12 jobs and 1 application note. "
      + "These jobs leave the Jobs page. Jobs in the Job pipeline stay, with their notes. You cannot undo this.")
  })

  it("names the missions of a schedule", () => {
    expect(deleteText("mandate", { ...preview, goals: 3, notes: 0 })).toBe(
      "This deletes the schedule, its chat, its 3 missions, 12 jobs and 0 application notes. "
      + "These jobs leave the Jobs page. Jobs in the Job pipeline stay, with their notes. You cannot undo this.")
  })

  it("does not talk about the Jobs page when no job goes", () => {
    expect(deleteText("goal", { ...preview, jobs: 0, notes: 0 })).toBe(
      "This deletes the mission, its chat, 0 jobs and 0 application notes. You cannot undo this.")
  })

  it("says that the agents stop first", () => {
    expect(deleteText("goal", { ...preview, jobs: 1, working: true })).toContain("1 job and 1 application note. These jobs")
    expect(deleteText("goal", { ...preview, working: true })).toContain("The agents that work on it stop first.")
  })
})
