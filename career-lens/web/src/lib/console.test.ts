import { describe, expect, it } from "vitest"
import { ago, statusTone, waitText } from "./format"
import { parseRoute, routePath } from "./route"
import { blankShift, shiftBody, shiftText, timeChoices } from "./shift"

describe("route", () => {
  it("reads and writes each page", () => {
    expect(parseRoute("/console")).toEqual({ section: "overview", requestId: null })
    expect(parseRoute("/console/requests/rq-1")).toEqual({ section: "requests", requestId: "rq-1" })
    expect(parseRoute("/console/nope")).toEqual({ section: "overview", requestId: null })
    expect(routePath({ section: "requests", requestId: "rq-1" })).toBe("/console/requests/rq-1")
    expect(routePath({ section: "overview", requestId: null })).toBe("/console")
  })
})

describe("format", () => {
  it("gives plain words for wait reasons, and shows a new reason", () => {
    expect(waitText("accounts_off_shift")).toBe("LinkedIn accounts are off shift")
    expect(waitText("something_new")).toBe("Something new")
    expect(waitText(null)).toBeNull()
  })
  it("gives a tone to each status", () => {
    expect(statusTone("satisfied")).toBe("good")
    expect(statusTone("failed")).toBe("bad")
    expect(statusTone("queued")).toBe("wait")
  })
  it("gives short ages", () => {
    const now = Date.parse("2026-01-02T00:00:00Z")
    expect(ago("2026-01-01T23:59:30Z", now)).toBe("30s ago")
    expect(ago("2026-01-01T22:00:00Z", now)).toBe("2h ago")
    expect(ago("2026-01-01T00:00:00Z", now)).toBe("1d ago")
  })
})

describe("shift", () => {
  it("keeps days with the same hours together", () => {
    expect(shiftText(blankShift())).toContain("Mon, Tue, Wed, Thu, Fri 10:00 to 19:00")
    expect(shiftText(null)).toContain("No shift")
  })
  it("sends no lunch for a day off", () => {
    const shift = blankShift()
    shift.days[0] = { ...shift.days[0], working: false, start_local: null, end_local: null }
    expect(shiftBody(shift).days[0].lunch_minutes).toBe(0)
  })
})

describe("shift times", () => {
  it("offers each 15 minutes, the end of the day, and a saved time that is not in the list", () => {
    expect(timeChoices(null)).toHaveLength(97)
    expect(timeChoices(null)).toContain("23:59")
    expect(timeChoices("09:10:00")).toContain("09:10")
  })
})
