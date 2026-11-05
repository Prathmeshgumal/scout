import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, expect, it, vi } from "vitest"
import { SignIn } from "./SignIn"

afterEach(() => vi.unstubAllGlobals())
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })

it("signs in with the password", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => json({ access_token: "made-up-token", token_type: "bearer", expires_in: 1 })))
  const onDone = vi.fn()
  render(<SignIn onDone={onDone} />)
  const button = screen.getByRole("button", { name: "Sign in" })
  expect(button).toBeDisabled()
  await userEvent.type(screen.getByLabelText("Password"), "made-up-password")
  await userEvent.click(button)
  expect(onDone).toHaveBeenCalledOnce()
})

it("shows the reason and stays open when the password is wrong", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => json({ detail: "The password is not correct." }, 401)))
  const onDone = vi.fn()
  render(<SignIn onDone={onDone} />)
  await userEvent.type(screen.getByLabelText("Password"), "wrong")
  await userEvent.click(screen.getByRole("button", { name: "Sign in" }))
  expect(await screen.findByRole("alert")).toHaveTextContent("The password is not correct.")
  expect(onDone).not.toHaveBeenCalled()
})
