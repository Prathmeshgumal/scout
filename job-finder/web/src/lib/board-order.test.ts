import { describe, expect, it } from "vitest"
import { inOrder, moveCard, placeVisible } from "./board-order"

const row = (key: string) => ({ key })
const keys = (rows: { key: string }[]) => rows.map((r) => r.key)

describe("inOrder", () => {
  it("keeps the order of the server when no card has a place", () => {
    expect(keys(inOrder([row("a"), row("b")], (r) => r.key, "fit", {}))).toEqual(["a", "b"])
  })

  it("puts the cards in their places, and new cards on top", () => {
    const positions = { a: { column: "fit", position: 1 }, b: { column: "fit", position: 0 } }
    expect(keys(inOrder([row("a"), row("new"), row("b")], (r) => r.key, "fit", positions))).toEqual(["new", "b", "a"])
  })

  it("ignores a place in another column: the card moved", () => {
    const positions = { a: { column: "applied", position: 0 }, b: { column: "fit", position: 0 } }
    expect(keys(inOrder([row("b"), row("a")], (r) => r.key, "fit", positions))).toEqual(["a", "b"])
  })
})

describe("moveCard", () => {
  it("moves a card up in its column", () => {
    expect(moveCard({ fit: ["a", "b", "c"] }, "c", "fit", 0)).toEqual({ fit: ["c", "a", "b"] })
  })

  it("moves a card to a place in another column", () => {
    expect(moveCard({ fit: ["a", "b"], applied: ["x", "y"] }, "a", "applied", 1)).toEqual({ fit: ["b"], applied: ["x", "a", "y"] })
  })

  it("puts a card at the end of a column when the place is past the end", () => {
    expect(moveCard({ fit: ["a"], applied: [] }, "a", "applied", 5)).toEqual({ fit: [], applied: ["a"] })
  })
})

describe("placeVisible", () => {
  it("keeps the hidden cards where they are", () => {
    expect(placeVisible(["a", "h1", "b", "h2", "c"], ["c", "a", "b"])).toEqual(["c", "h1", "a", "h2", "b"])
  })

  it("adds visible cards that had no place at the end", () => {
    expect(placeVisible(["a"], ["n", "a"])).toEqual(["n", "a"])
  })
})
