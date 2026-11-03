import { render, screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, expect, it, vi } from "vitest"
import { RequestDetailView } from "./RequestDetailView"

const detail = (status: string, cancel_requested_at: string | null = null) => ({
  request_id: "rq-1", kind: "linkedin_company", status, label: null, url: "https://example.com", query: null,
  requested_at: "2026-11-05T10:00:00Z", started_at: null, finished_at: null, failure: null, max_attempts: 3,
  next_attempt_at: null, wait_reason: null, cancel_requested_at, satisfied_at: null, attempts: [], captures: [],
  extraction: null, sections: [], events: [], screenshots: [],
})

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })

afterEach(() => vi.unstubAllGlobals())

it("cancels a running request after the operator agrees", async () => {
  let cancelled = false
  const fetcher = vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === "POST") { cancelled = true; return json({ status: "running", cancel: "stopping" }, 202) }
    return json(detail("running", cancelled ? "2026-11-05T10:01:00Z" : null))
  })
  vi.stubGlobal("fetch", fetcher)
  render(<RequestDetailView id="rq-1" onBack={() => {}} />)
  await userEvent.click(await screen.findByRole("button", { name: "Cancel the request" }))
  const dialog = await screen.findByRole("alertdialog")
  await userEvent.click(within(dialog).getByRole("button", { name: "Cancel the request" }))
  const posted = fetcher.mock.calls.find(([, init]) => init?.method === "POST")
  expect(posted?.[0]).toBe("/operator/api/requests/rq-1/cancel")
  expect(await screen.findByText("Stopping")).toBeInTheDocument()
  expect(screen.queryByRole("button", { name: "Cancel the request" })).toBeNull()
})

it("keeps the request when the operator says no", async () => {
  const fetcher = vi.fn(async (_url: string, _init?: RequestInit) => json(detail("queued")))
  vi.stubGlobal("fetch", fetcher)
  render(<RequestDetailView id="rq-1" onBack={() => {}} />)
  await userEvent.click(await screen.findByRole("button", { name: "Cancel the request" }))
  await userEvent.click(await screen.findByRole("button", { name: "Keep it" }))
  expect(fetcher.mock.calls.every(([, init]) => init?.method !== "POST")).toBe(true)
})

it("shows no cancel button on a finished request", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => json(detail("succeeded"))))
  render(<RequestDetailView id="rq-1" onBack={() => {}} />)
  await screen.findAllByText("https://example.com")
  expect(screen.queryByRole("button", { name: "Cancel the request" })).toBeNull()
})
