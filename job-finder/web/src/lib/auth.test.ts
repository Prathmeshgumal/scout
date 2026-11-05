import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { ApiError, clearToken, download, listGoals, readToken, saveToken, signIn, watchGoal, whenSignedOut } from "./api"

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })

beforeEach(() => clearToken())
afterEach(() => {
  vi.unstubAllGlobals()
  whenSignedOut(() => {})
})

const sentAuth = (fetcher: ReturnType<typeof vi.fn>) => new Headers((fetcher.mock.calls[0][1] as RequestInit).headers).get("Authorization")

it("keeps the token that the password gives", async () => {
  const fetcher = vi.fn(async () => json({ access_token: "made-up-token", token_type: "bearer", expires_in: 43200 }))
  vi.stubGlobal("fetch", fetcher)
  await signIn("made-up-password")
  const [url, init] = fetcher.mock.calls[0] as unknown as [string, RequestInit]
  expect(url).toBe("/api/auth/login")
  expect(JSON.parse(String(init.body))).toEqual({ password: "made-up-password" })
  expect(readToken()).toBe("made-up-token")
})

it("shows why a sign-in failed and keeps no token", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => json({ detail: "The password is not correct." }, 401)))
  const signedOut = vi.fn()
  whenSignedOut(signedOut)
  await expect(signIn("wrong")).rejects.toThrow("The password is not correct.")
  expect(readToken()).toBeNull()
  expect(signedOut).not.toHaveBeenCalled()
})

it("sends the token on a call", async () => {
  saveToken("made-up-token")
  const fetcher = vi.fn(async () => json([]))
  vi.stubGlobal("fetch", fetcher)
  await listGoals()
  expect(sentAuth(fetcher)).toBe("Bearer made-up-token")
})

it("sends the token on the stream of a goal", async () => {
  saveToken("made-up-token")
  const fetcher = vi.fn(async () => new Response("", { status: 200 }))
  vi.stubGlobal("fetch", fetcher)
  await watchGoal("g-1", 0, () => {})
  expect(sentAuth(fetcher)).toBe("Bearer made-up-token")
})

it("forgets the token and goes to the sign-in on a 401", async () => {
  saveToken("made-up-token")
  vi.stubGlobal("fetch", vi.fn(async () => json({ detail: "Sign in to job-finder." }, 401)))
  const signedOut = vi.fn()
  whenSignedOut(signedOut)
  await expect(listGoals()).rejects.toBeInstanceOf(ApiError)
  expect(readToken()).toBeNull()
  expect(signedOut).toHaveBeenCalledOnce()
})

it("downloads a file with the token and uses the name from the server", async () => {
  saveToken("made-up-token")
  const body = new Response("data", { headers: { "Content-Disposition": 'attachment; filename="cv.pdf"' } })
  const fetcher = vi.fn(async () => body)
  vi.stubGlobal("fetch", fetcher)
  const url = { createObjectURL: vi.fn(() => "blob:x"), revokeObjectURL: vi.fn() }
  vi.stubGlobal("URL", Object.assign(URL, url))
  const clicks: string[] = []
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) { clicks.push(this.download) })
  await download("/api/files/f-1/download")
  expect(sentAuth(fetcher)).toBe("Bearer made-up-token")
  expect(clicks).toEqual(["cv.pdf"])
  expect(url.revokeObjectURL).toHaveBeenCalledWith("blob:x")
})

it("reads the encoded name that the server gives a stored file", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response("data", { headers: { "Content-Disposition": "attachment; filename*=UTF-8''my%20r%C3%A9sum%C3%A9.pdf" } })))
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: vi.fn(() => "blob:x"), revokeObjectURL: vi.fn() }))
  const clicks: string[] = []
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) { clicks.push(this.download) })
  await download("/api/files/f-1/download")
  expect(clicks).toEqual(["my résumé.pdf"])
})

it("reports a download that failed", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => json({ detail: "No such file." }, 404)))
  await expect(download("/api/files/none/download")).rejects.toThrow("No such file.")
})
