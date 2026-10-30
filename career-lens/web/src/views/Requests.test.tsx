import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"
import { Requests } from "./Requests"

const page = {
  items: [{ id: "rq-1", kind: "linkedin_job", status: "queued", label: null, target: "https://x.test/1", requested_at: new Date().toISOString(), wait_reason: "accounts_off_shift", attempts: 0 }],
  counts: { queued: 1 }, next: null,
}

describe("Requests", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(page))))
  })

  it("shows a request with its wait reason, and opens it", async () => {
    const onOpen = vi.fn()
    render(<Requests onOpen={onOpen} />)
    await userEvent.click(await screen.findByRole("button", { name: /https:\/\/x.test\/1/ }))
    expect(screen.getByText(/LinkedIn accounts are off shift/)).toBeTruthy()
    expect(onOpen).toHaveBeenCalledWith("rq-1")
  })

  it("sends the status filter to the server", async () => {
    render(<Requests onOpen={() => {}} />)
    await userEvent.click(await screen.findByRole("button", { name: /Failed/ }))
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("status=failed"))).toBe(true))
  })
})
