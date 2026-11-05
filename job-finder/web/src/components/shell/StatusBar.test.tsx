import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"
import type { Status } from "@/lib/api"
import { StatusBar } from "./StatusBar"

const status = (over: Partial<Status> = {}): Status => ({
  anthropic_configured: true, claude_login: "Claude subscription token", career_lens_configured: true, open_sessions: 0, ...over,
})
const bar = (s: Status | null, onLogin = vi.fn()) =>
  render(<StatusBar status={s} wait={null} spend={null} running={0} onCommand={() => {}} onLogin={onLogin} />)

describe("status bar", () => {
  it("shows the login in use and opens the settings", async () => {
    const onLogin = vi.fn()
    bar(status(), onLogin)
    await userEvent.click(screen.getByRole("button", { name: "Claude login: Claude Code" }))
    expect(onLogin).toHaveBeenCalledOnce()
  })

  it("shows an API key and a missing login", () => {
    const { unmount } = bar(status({ claude_login: "API key" }))
    expect(screen.getByRole("button", { name: "Claude login: API key" })).toBeInTheDocument()
    unmount()
    bar(status({ anthropic_configured: false, claude_login: "Claude not connected" }))
    expect(screen.getByRole("button", { name: "Claude login: Not connected" })).toBeInTheDocument()
  })
})
