import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { useState } from "react"
import { beforeEach, describe, expect, it, vi } from "vitest"
import * as api from "@/lib/api"
import { TooltipProvider } from "@/components/ui/tooltip"
import { LoginPopover } from "./LoginPopover"

vi.mock("@/lib/api", async (original) => ({
  ...(await original<typeof api>()),
  getStatus: vi.fn(),
  saveLogin: vi.fn(),
  chooseLogin: vi.fn(),
  startSubscriptionLogin: vi.fn(),
  finishSubscriptionLogin: vi.fn(),
}))

const URL_OF_CLAUDE = "https://claude.ai/oauth/authorize?code=true"

function status(login: string, saved: Partial<Record<api.LoginMode, boolean>> = {}) {
  return {
    anthropic_configured: login !== "Claude not connected",
    claude_login: login,
    claude_account: { loaded_at: "old", saved: { api_key: false, oauth_token: false, ...saved } },
  } as unknown as api.Status
}

function Page({ current, onChanged = () => {} }: { current: api.Status; onChanged?: () => void }) {
  const [open, setOpen] = useState(true)
  return (
    <TooltipProvider>
      <LoginPopover open={open} onOpenChange={setOpen} status={current} onChanged={onChanged} restartPollMs={1} />
    </TooltipProvider>
  )
}

let opened: { location: { href: string }; opener: unknown; close: ReturnType<typeof vi.fn> }

beforeEach(() => {
  vi.resetAllMocks()
  opened = { location: { href: "about:blank" }, opener: {}, close: vi.fn() }
  vi.spyOn(window, "open").mockReturnValue(opened as unknown as Window)
  vi.mocked(api.getStatus).mockResolvedValue({ claude_account: { loaded_at: "new" } } as api.Status)
  vi.mocked(api.startSubscriptionLogin).mockResolvedValue({ url: URL_OF_CLAUDE })
})

describe("LoginPopover", () => {
  it("shows the toggle on the login in use, with only one action", () => {
    render(<Page current={status("Claude subscription token", { oauth_token: true })} />)
    expect(screen.getByRole("radio", { name: /Claude Code/ })).toHaveAttribute("aria-checked", "true")
    expect(screen.getByRole("radio", { name: /API key/ })).toHaveAttribute("aria-checked", "false")
    expect(screen.getByRole("button", { name: "Use another account" })).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /remove/i })).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/label/i)).not.toBeInTheDocument()
  })

  it("opens the Claude page and finishes with the pasted code", async () => {
    vi.mocked(api.finishSubscriptionLogin).mockResolvedValue({ restarting: true, mode: "oauth_token" })
    const onChanged = vi.fn()
    render(<Page current={status("Claude not connected")} onChanged={onChanged} />)
    await userEvent.click(screen.getByRole("button", { name: "Use Claude Code" }))
    await screen.findByPlaceholderText("Paste the code")
    expect(window.open).toHaveBeenCalledWith("about:blank", "_blank")
    expect(opened.location.href).toBe(URL_OF_CLAUDE)
    expect(opened.opener).toBeNull()
    await userEvent.type(screen.getByPlaceholderText("Paste the code"), " the-code ")
    await userEvent.click(screen.getByRole("button", { name: "Done" }))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect(api.finishSubscriptionLogin).toHaveBeenCalledWith("the-code")
    await waitFor(() => expect(screen.queryByPlaceholderText("Paste the code")).not.toBeInTheDocument())
  })

  it("gives a link when the browser stops the new tab", async () => {
    vi.mocked(window.open).mockReturnValue(null)
    render(<Page current={status("Claude not connected")} />)
    await userEvent.click(screen.getByRole("button", { name: "Use Claude Code" }))
    expect(await screen.findByRole("link", { name: "Open the sign-in page" })).toHaveAttribute("href", URL_OF_CLAUDE)
  })

  it("closes the blank tab when the sign-in does not start", async () => {
    vi.mocked(api.startSubscriptionLogin).mockRejectedValue(new Error("Claude did not start."))
    render(<Page current={status("Claude not connected")} />)
    await userEvent.click(screen.getByRole("button", { name: "Use Claude Code" }))
    expect(await screen.findByRole("alert")).toHaveTextContent("Claude did not start.")
    expect(opened.close).toHaveBeenCalled()
  })

  it("goes back to the button after a wrong code, because that sign-in has ended", async () => {
    vi.mocked(api.finishSubscriptionLogin).mockRejectedValue(new Error("Claude did not accept the code. Sign in again."))
    render(<Page current={status("Claude not connected")} />)
    await userEvent.click(screen.getByRole("button", { name: "Use Claude Code" }))
    await userEvent.type(await screen.findByPlaceholderText("Paste the code"), "bad")
    await userEvent.click(screen.getByRole("button", { name: "Done" }))
    expect(await screen.findByRole("alert")).toHaveTextContent("Sign in again")
    expect(screen.getByRole("button", { name: "Use Claude Code" })).toBeInTheDocument()
    expect(screen.queryByPlaceholderText("Paste the code")).not.toBeInTheDocument()
  })

  it("uses a saved subscription with no sign-in", async () => {
    vi.mocked(api.chooseLogin).mockResolvedValue({ restarting: true, mode: "oauth_token" })
    render(<Page current={status("API key", { api_key: true, oauth_token: true })} />)
    await userEvent.click(screen.getByRole("radio", { name: /Claude Code/ }))
    await userEvent.click(screen.getByRole("button", { name: "Use Claude Code" }))
    await waitFor(() => expect(api.chooseLogin).toHaveBeenCalledWith("oauth_token"))
    expect(window.open).not.toHaveBeenCalled()
    expect(api.startSubscriptionLogin).not.toHaveBeenCalled()
  })

  it("saves a pasted API key", async () => {
    vi.mocked(api.saveLogin).mockResolvedValue({ restarting: true, mode: "api_key" })
    render(<Page current={status("Claude subscription token", { oauth_token: true })} />)
    await userEvent.click(screen.getByRole("radio", { name: /API key/ }))
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled()
    await userEvent.type(screen.getByPlaceholderText("Paste your API key"), "sk-ant-api03-x")
    await userEvent.click(screen.getByRole("button", { name: "Save" }))
    await waitFor(() => expect(api.saveLogin).toHaveBeenCalledWith("sk-ant-api03-x"))
  })

  it("uses a saved API key when the box is empty", async () => {
    vi.mocked(api.chooseLogin).mockResolvedValue({ restarting: true, mode: "api_key" })
    render(<Page current={status("Claude subscription token", { api_key: true, oauth_token: true })} />)
    await userEvent.click(screen.getByRole("radio", { name: /API key/ }))
    await userEvent.click(screen.getByRole("button", { name: "Use saved key" }))
    await waitFor(() => expect(api.chooseLogin).toHaveBeenCalledWith("api_key"))
  })

  it("says to restart when the app cannot restart itself", async () => {
    vi.mocked(api.saveLogin).mockResolvedValue({ restarting: false, mode: "api_key" })
    const onChanged = vi.fn()
    render(<Page current={status("Claude not connected")} onChanged={onChanged} />)
    await userEvent.click(screen.getByRole("radio", { name: /API key/ }))
    await userEvent.type(screen.getByPlaceholderText("Paste your API key"), "sk-ant-api03-x")
    await userEvent.click(screen.getByRole("button", { name: "Save" }))
    expect(await screen.findByRole("status")).toHaveTextContent("Restart")
    expect(onChanged).toHaveBeenCalled()
  })
})
