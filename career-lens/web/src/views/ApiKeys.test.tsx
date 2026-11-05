import { render, screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, expect, it, vi } from "vitest"
import type { ApiKey } from "@/lib/api"
import { ApiKeys } from "./ApiKeys"

const active: ApiKey = {
  id: "ak-1", name: "Job finder", prefix: "cl_v1.ak-1", created_at: "2026-11-04T16:25:00Z", revoked_at: null,
}
const revoked: ApiKey = {
  id: "ak-2", name: "Old test", prefix: "cl_v1.ak-2", created_at: "2026-11-03T16:25:00Z", revoked_at: "2026-11-04T10:00:00Z",
}
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })

function server(keys: ApiKey[], answer: () => Response = () => json({})) {
  const fetcher = vi.fn(async (_url: string, init?: RequestInit) => (init?.method ? answer() : json(keys)))
  vi.stubGlobal("fetch", fetcher)
  return fetcher
}
const sent = (fetcher: ReturnType<typeof server>) => fetcher.mock.calls.filter(([, init]) => init?.method)

afterEach(() => vi.unstubAllGlobals())

it("lists the keys and lets only an active key be revoked", async () => {
  server([active, revoked])
  render(<ApiKeys />)
  const live = await screen.findByRole("listitem", { name: "Job finder" })
  expect(within(live).getByText("Active")).toBeInTheDocument()
  expect(within(live).getByRole("button", { name: "Revoke" })).toBeInTheDocument()
  const old = screen.getByRole("listitem", { name: "Old test" })
  expect(within(old).getByText("Revoked")).toBeInTheDocument()
  expect(within(old).queryByRole("button", { name: "Revoke" })).toBeNull()
})

it("says so when there are no keys", async () => {
  server([])
  render(<ApiKeys />)
  expect(await screen.findByText("There are no API keys.")).toBeInTheDocument()
})

it("shows a new key one time and forgets it when closed", async () => {
  const fetcher = server([], () => json({ ...active, token: "cl_v1.ak-1.made-up-secret" }, 201))
  render(<ApiKeys />)
  await userEvent.click(await screen.findByRole("button", { name: "Create key" }))
  const dialog = await screen.findByRole("dialog")
  await userEvent.type(within(dialog).getByLabelText("Name"), "Job finder")
  await userEvent.click(within(dialog).getByRole("button", { name: "Create key" }))

  expect(await within(dialog).findByLabelText("New API key")).toHaveValue("cl_v1.ak-1.made-up-secret")
  expect(within(dialog).getByText(/cannot see it again/)).toBeInTheDocument()
  const [url, init] = sent(fetcher)[0]
  expect(url).toBe("/operator/api/api-keys")
  expect(JSON.parse(String(init?.body))).toEqual({ name: "Job finder" })

  await userEvent.click(within(dialog).getByRole("button", { name: "Done" }))
  expect(screen.queryByRole("dialog")).toBeNull()
  await userEvent.click(screen.getByRole("button", { name: "Create key" }))
  expect(within(await screen.findByRole("dialog")).queryByLabelText("New API key")).toBeNull()
})

it("does not close on an outside click while the new key shows", async () => {
  server([], () => json({ ...active, token: "cl_v1.ak-1.made-up-secret" }, 201))
  render(<ApiKeys />)
  await userEvent.click(await screen.findByRole("button", { name: "Create key" }))
  const dialog = await screen.findByRole("dialog")
  await userEvent.type(within(dialog).getByLabelText("Name"), "Job finder")
  await userEvent.click(within(dialog).getByRole("button", { name: "Create key" }))
  await within(dialog).findByLabelText("New API key")
  await userEvent.keyboard("{Escape}")
  expect(screen.getByRole("dialog")).toBeInTheDocument()
})

it("copies the new key", async () => {
  server([], () => json({ ...active, token: "cl_v1.ak-1.made-up-secret" }, 201))
  const user = userEvent.setup()
  render(<ApiKeys />)
  await user.click(await screen.findByRole("button", { name: "Create key" }))
  const dialog = await screen.findByRole("dialog")
  await user.type(within(dialog).getByLabelText("Name"), "Job finder")
  await user.click(within(dialog).getByRole("button", { name: "Create key" }))
  await user.click(await within(dialog).findByRole("button", { name: "Copy key" }))
  expect(await navigator.clipboard.readText()).toBe("cl_v1.ak-1.made-up-secret")
})

it("keeps the dialog open and shows why a key was not made", async () => {
  server([], () => json({ detail: "API key name must contain 1-120 characters" }, 422))
  render(<ApiKeys />)
  await userEvent.click(await screen.findByRole("button", { name: "Create key" }))
  const dialog = await screen.findByRole("dialog")
  await userEvent.type(within(dialog).getByLabelText("Name"), "Bad")
  await userEvent.click(within(dialog).getByRole("button", { name: "Create key" }))
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("1-120 characters")
})

it("asks first, then revokes the key", async () => {
  const fetcher = server([active], () => json({ ...active, revoked_at: "2026-11-05T10:00:00Z" }))
  render(<ApiKeys />)
  await userEvent.click(await screen.findByRole("button", { name: "Revoke" }))
  const dialog = await screen.findByRole("alertdialog")
  expect(within(dialog).getByText(/lose access at once/)).toBeInTheDocument()
  expect(sent(fetcher)).toHaveLength(0)
  await userEvent.click(within(dialog).getByRole("button", { name: "Revoke key" }))
  expect(sent(fetcher)[0][0]).toBe("/operator/api/api-keys/ak-1/revoke")
  expect(sent(fetcher)[0][1]?.method).toBe("POST")
})

it("shows a problem when the keys do not load", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => json({ detail: "The server failed." }, 500)))
  render(<ApiKeys />)
  expect(await screen.findByRole("alert")).toHaveTextContent("The server failed.")
})
