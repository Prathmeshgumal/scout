import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, expect, it, vi } from "vitest"
import { DeleteDialog } from "./DeleteDialog"

const target = { kind: "goal" as const, id: "g-1", title: "Go roles" }
const body = { goals: 1, jobs: 7, notes: 0, working: false }
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status })

afterEach(() => vi.unstubAllGlobals())

it("keeps Delete off until it knows what goes, then names the jobs", async () => {
  let answer = (_: Response) => {}
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { answer = resolve })))
  render(<DeleteDialog target={target} onClose={() => {}} onDeleted={() => {}} />)
  expect(screen.getByRole("button", { name: "Delete" })).toBeDisabled()
  expect(screen.getByText("Reading what this deletes")).toBeInTheDocument()
  answer(json(body))
  expect(await screen.findByText(/7 jobs/)).toBeInTheDocument()
  expect(screen.getByRole("button", { name: "Delete" })).toBeEnabled()
})

it("closes when it cannot read what goes", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => json({ detail: "no" }, 500)))
  const onClose = vi.fn()
  render(<DeleteDialog target={target} onClose={onClose} onDeleted={() => {}} />)
  await waitFor(() => expect(onClose).toHaveBeenCalled())
})

it("deletes after the click", async () => {
  const fetcher = vi.fn(async (_url: string, init?: RequestInit) => (
    init?.method === "DELETE" ? new Response(null, { status: 204 }) : json(body)))
  vi.stubGlobal("fetch", fetcher)
  const onDeleted = vi.fn()
  render(<DeleteDialog target={target} onClose={() => {}} onDeleted={onDeleted} />)
  await screen.findByText(/7 jobs/)
  await userEvent.click(screen.getByRole("button", { name: "Delete" }))
  await waitFor(() => expect(onDeleted).toHaveBeenCalledWith(target))
})
