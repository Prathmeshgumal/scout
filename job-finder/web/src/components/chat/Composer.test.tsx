import { fireEvent, render, screen } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"
import type { FileInfo } from "@/lib/api"
import { Composer } from "./Composer"

const file = (over: Partial<FileInfo> = {}): FileInfo => ({
  id: "f1", goal_id: null, role: "attachment", name: "notes.md", kind: "text", size_bytes: 5, readable: true, ...over,
})

describe("Composer files", () => {
  it("shows attached files and removes one", () => {
    const onRemove = vi.fn()
    render(<Composer value="" onChange={() => {}} onSubmit={() => {}} attached={[file()]} onRemove={onRemove} />)
    fireEvent.click(screen.getByRole("button", { name: "Remove notes.md" }))
    expect(onRemove).toHaveBeenCalledWith("f1")
  })

  it("says when agents cannot read a file", () => {
    render(<Composer value="" onChange={() => {}} onSubmit={() => {}} attached={[file({ readable: false })]} />)
    expect(screen.getByText("(agents cannot read this)")).toBeTruthy()
  })

  it("waits for an upload before it sends", () => {
    render(<Composer value="hello" onChange={() => {}} onSubmit={() => {}} uploading />)
    expect((screen.getByRole("button", { name: "Send" }) as HTMLButtonElement).disabled).toBe(true)
  })
})
