import { describe, expect, it } from "vitest"
import { splitChoices } from "./choices"

describe("splitChoices", () => {
  it("takes the choices from the last line of a reply", () => {
    expect(splitChoices("| Status | active |\n\nStart it?\n[[Start mandate | Change settings]]")).toEqual({
      text: "| Status | active |\n\nStart it?",
      choices: [{ label: "Start mandate", danger: false }, { label: "Change settings", danger: false }],
    })
  })

  it("ignores spaces and empty lines after the choices", () => {
    expect(splitChoices("Save this change?\n  [[ Save change |Discard ]]  \n\n")).toEqual({
      text: "Save this change?",
      choices: [{ label: "Save change", danger: false }, { label: "Discard", danger: false }],
    })
  })

  it("keeps a text with no choices as it is", () => {
    expect(splitChoices("Research finished on 5 roles.")).toEqual({ text: "Research finished on 5 roles.", choices: [] })
  })

  it("keeps a marker that is not on the last line", () => {
    const text = "[[Yes | No]]\nMore text."
    expect(splitChoices(text)).toEqual({ text, choices: [] })
  })

  it("keeps a marker in the middle of a line", () => {
    const text = "Write [[Yes | No]]"
    expect(splitChoices(text)).toEqual({ text, choices: [] })
  })

  it("hides a marker that is still being written", () => {
    expect(splitChoices("Start it?\n[[Start mand")).toEqual({ text: "Start it?", choices: [] })
  })

  it("drops empty choices and keeps at most four", () => {
    expect(splitChoices("Pick one.\n[[A | | B | C | D | E]]").choices.map((c) => c.label)).toEqual(["A", "B", "C", "D"])
  })

  it("marks a choice that starts with ! as one that cannot be undone", () => {
    expect(splitChoices("End it?\n[[ !End mandate | Keep mandate]]").choices).toEqual([
      { label: "End mandate", danger: true }, { label: "Keep mandate", danger: false },
    ])
  })
})
