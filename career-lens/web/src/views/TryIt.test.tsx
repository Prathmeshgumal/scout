import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { expect, it, vi } from "vitest"
import { TryIt } from "./TryIt"

it("sends the right field for a job search, and opens the request", async () => {
  const fetcher = vi.fn(async () => new Response(JSON.stringify({ request_id: "rq-9" })))
  vi.stubGlobal("fetch", fetcher)
  const onOpen = vi.fn()
  render(<TryIt onOpen={onOpen} />)
  await userEvent.selectOptions(screen.getByLabelText("Type"), "linkedin-job-search")
  await userEvent.type(screen.getByLabelText("Search words"), "go engineer")
  await userEvent.click(screen.getByRole("button", { name: "Send the request" }))
  expect(onOpen).toHaveBeenCalledWith("rq-9")
  const [url, init] = fetcher.mock.calls[0] as unknown as [string, RequestInit]
  expect(url).toBe("/operator/api/try/linkedin-job-search")
  expect(JSON.parse(String(init.body))).toEqual({ keywords: "go engineer" })
})
