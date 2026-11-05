import { render, screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, expect, it, vi } from "vitest"
import type { Account, Overview } from "@/lib/api"
import { Accounts } from "./Accounts"

const account: Account = {
  id: "la-1", label: "Main", username: "main@example.com", state: "enabled", max_attempts_24h: 20,
  min_interval_seconds: 3, cooldown_until: null, attention_detail: null, assignment: null, schedule: null, attempts_24h: 15,
}
const data: Overview = { workers: [], accounts: [account], requests: [] }

afterEach(() => vi.unstubAllGlobals())

async function open() {
  render(<Accounts data={data} error={null} onChange={onChange} />)
  await userEvent.click(screen.getByRole("button", { name: "Edit limits" }))
  return screen.findByRole("dialog")
}
const onChange = vi.fn()

it("saves new limits", async () => {
  const fetcher = vi.fn(async () => new Response(JSON.stringify({ account_id: "la-1", max_attempts_24h: 35, min_interval_seconds: 45 })))
  vi.stubGlobal("fetch", fetcher)
  const dialog = await open()
  const scrapes = within(dialog).getByLabelText("Scrapes in 24 hours")
  const gap = within(dialog).getByLabelText("Seconds between scrapes")
  expect(scrapes).toHaveValue(20)
  expect(gap).toHaveValue(3)
  await userEvent.clear(scrapes)
  await userEvent.type(scrapes, "35")
  await userEvent.clear(gap)
  await userEvent.type(gap, "45")
  await userEvent.click(within(dialog).getByRole("button", { name: "Save limits" }))
  const [url, init] = fetcher.mock.calls[0] as unknown as [string, RequestInit]
  expect(url).toBe("/operator/api/accounts/la-1/limits")
  expect(init.method).toBe("PUT")
  expect(JSON.parse(String(init.body))).toEqual({ max_attempts_24h: 35, min_interval_seconds: 45 })
  expect(onChange).toHaveBeenCalled()
  expect(screen.queryByRole("dialog")).toBeNull()
})

it("does not save a limit below 1", async () => {
  const fetcher = vi.fn()
  vi.stubGlobal("fetch", fetcher)
  const dialog = await open()
  const scrapes = within(dialog).getByLabelText("Scrapes in 24 hours")
  await userEvent.clear(scrapes)
  await userEvent.type(scrapes, "0")
  expect(within(dialog).getByRole("button", { name: "Save limits" })).toBeDisabled()
  expect(fetcher).not.toHaveBeenCalled()
})

it("keeps the dialog open and shows the reason when the server refuses", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "this account is retired" }), { status: 409 })))
  const dialog = await open()
  await userEvent.click(within(dialog).getByRole("button", { name: "Save limits" }))
  expect(await within(dialog).findByText("this account is retired")).toBeInTheDocument()
})
