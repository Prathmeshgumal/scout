import { render, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"
import { StatusBar } from "@/components/shell/StatusBar"
import type { Status } from "@/lib/api"
import { SettingsPage } from "./SettingsPage"

const account = { account_label: "Personal", saved_at: "2026-11-04T19:21:54Z", loaded_at: "2026-11-05T00:00:00Z", restart_required: false, check_error: false }
const status = (over: Partial<Status> = {}, saved = { api_key: false, oauth_token: true }): Status => ({
  anthropic_configured: true, claude_login: "Claude subscription token", career_lens_configured: true, open_sessions: 0,
  claude_account: { ...account, saved }, ...over,
})

function serve(routes: Record<string, { status?: number; body: unknown }>) {
  const fetcher = vi.fn(async (url: string, _init?: RequestInit) => {
    const route = routes[url] ?? { body: status({ claude_account: { ...account, loaded_at: "2026-11-05T01:00:00Z" } }) }
    return new Response(JSON.stringify(route.body), { status: route.status ?? 200 })
  })
  vi.stubGlobal("fetch", fetcher)
  return fetcher
}

const bodyOf = (fetcher: ReturnType<typeof serve>, url: string) => {
  const call = fetcher.mock.calls.find(([u]) => u === url)
  return call && JSON.parse(String(call[1]?.body))
}

const show = (s = status(), onChanged = vi.fn()) => render(<SettingsPage status={s} onChanged={onChanged} restartPollMs={1} />)
const subscription = () => screen.getByRole("region", { name: "Claude subscription" })
const apiKey = () => screen.getByRole("region", { name: "Anthropic API key" })

describe("Settings", () => {
  beforeEach(() => vi.unstubAllGlobals())

  it("shows the login in use and its account label", () => {
    serve({})
    show()
    expect(within(subscription()).getByText("In use")).toBeInTheDocument()
    expect(within(subscription()).getByText(/Personal/)).toBeInTheDocument()
    expect(within(apiKey()).getByText("Not saved")).toBeInTheDocument()
  })

  it("offers another account when a subscription is saved", () => {
    serve({})
    show()
    expect(within(subscription()).getByRole("button", { name: "Sign in with another account" })).toBeInTheDocument()
    expect(within(subscription()).getByText(/sign in to that account/i)).toBeInTheDocument()
  })

  it("offers the first sign-in when nothing is saved", () => {
    serve({})
    show(status({ anthropic_configured: false, claude_login: "Claude not connected" }, { api_key: false, oauth_token: false }))
    expect(within(subscription()).getByRole("button", { name: "Sign in with Claude" })).toBeInTheDocument()
  })

  it("saves an API key from a hidden field and clears the field", async () => {
    const fetcher = serve({ "/api/login": { body: { mode: "api_key", restarting: false } } })
    show()
    const field = screen.getByLabelText("API key")
    expect(field).toHaveAttribute("type", "password")
    await userEvent.type(field, "sk-ant-api03-abc")
    await userEvent.click(screen.getByRole("button", { name: "Save the API key" }))
    await waitFor(() => expect(bodyOf(fetcher, "/api/login")?.credential).toBe("sk-ant-api03-abc"))
    expect(await screen.findByText(/Restart `job-finder serve`/)).toBeInTheDocument()
    expect(field).toHaveValue("")
  })

  it("shows the error of the server", async () => {
    serve({ "/api/login": { status: 400, body: { detail: "That does not look like a Claude token or an Anthropic API key." } } })
    show()
    await userEvent.type(screen.getByLabelText("API key"), "wrong")
    await userEvent.click(screen.getByRole("button", { name: "Save the API key" }))
    expect(await screen.findByRole("alert")).toHaveTextContent("does not look like")
  })

  it("changes the account: the link, the code and the label, then the restart", async () => {
    const onChanged = vi.fn()
    const fetcher = serve({
      "/api/login/subscription/start": { body: { url: "https://claude.com/cai/oauth/authorize?x=1" } },
      "/api/login/subscription/code": { body: { mode: "oauth_token", restarting: true } },
    })
    show(status(), onChanged)
    await userEvent.click(screen.getByRole("button", { name: "Sign in with another account" }))
    const link = await screen.findByRole("link", { name: /Open the Claude sign-in page/ })
    expect(link).toHaveAttribute("href", "https://claude.com/cai/oauth/authorize?x=1")
    expect(link).toHaveAttribute("rel", expect.stringContaining("noopener"))
    await userEvent.type(screen.getByLabelText("Code from Claude"), "the-code")
    await userEvent.type(within(subscription()).getByLabelText("Account label (optional)"), "Work")
    await userEvent.click(screen.getByRole("button", { name: "Finish the sign-in" }))
    await waitFor(() => expect(bodyOf(fetcher, "/api/login/subscription/code")).toEqual({ code: "the-code", account_label: "Work" }))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
  })

  it("lets the user paste a subscription token", async () => {
    const fetcher = serve({ "/api/login": { body: { mode: "oauth_token", restarting: false } } })
    show()
    await userEvent.type(screen.getByLabelText("Token from claude setup-token"), "sk-ant-oat01-abc")
    await userEvent.click(screen.getByRole("button", { name: "Save the token" }))
    await waitFor(() => expect(bodyOf(fetcher, "/api/login")?.credential).toBe("sk-ant-oat01-abc"))
  })

  it("says that a saved API key wins over the subscription", () => {
    serve({})
    show(status({ claude_login: "API key" }, { api_key: true, oauth_token: true }))
    expect(within(subscription()).getByText("Saved, not in use")).toBeInTheDocument()
    expect(within(subscription()).getByText(/Remove the API key to use the subscription/)).toBeInTheDocument()
    expect(within(apiKey()).getByText("In use")).toBeInTheDocument()
  })

  it("removes a saved API key only after a second click", async () => {
    const fetcher = serve({ "/api/login/api_key": { body: { restarting: false } } })
    show(status({ claude_login: "API key" }, { api_key: true, oauth_token: true }))
    await userEvent.click(screen.getByRole("button", { name: "Remove the API key" }))
    expect(fetcher.mock.calls.some(([u]) => u === "/api/login/api_key")).toBe(false)
    await userEvent.click(screen.getByRole("button", { name: "Yes, remove the API key" }))
    await waitFor(() => expect(fetcher.mock.calls.find(([u]) => u === "/api/login/api_key")?.[1]?.method).toBe("DELETE"))
  })

  it("says when a saved login needs a restart", () => {
    serve({})
    show(status({ claude_account: { ...account, restart_required: true } }))
    expect(screen.getByText(/saved login is not in use/i)).toBeInTheDocument()
  })
})

describe("status bar", () => {
  const bar = (s: Status | null, onLogin = vi.fn()) =>
    render(<StatusBar status={s} wait={null} spend={null} running={0} onCommand={() => {}} onLogin={onLogin} />)

  it("shows the login in use and opens the settings", async () => {
    const onLogin = vi.fn()
    bar(status(), onLogin)
    await userEvent.click(screen.getByRole("button", { name: "Claude login: Subscription" }))
    expect(onLogin).toHaveBeenCalledOnce()
  })

  it("shows when Claude is not connected", () => {
    bar(status({ anthropic_configured: false, claude_login: "Claude not connected" }))
    expect(screen.getByRole("button", { name: "Claude login: Not connected" })).toBeInTheDocument()
  })
})
