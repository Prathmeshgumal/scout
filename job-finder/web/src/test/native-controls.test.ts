import { expect, it } from "vitest"

const NATIVE = /<select\b|<option\b|<datalist\b|type="(checkbox|radio|date|datetime-local|month|week|color|range)"/
const FILES = import.meta.glob("../**/*.tsx", { query: "?raw", import: "default", eager: true }) as Record<string, string>

// The open list of a native control ignores the theme of the app. Use components/ui.
it("uses no native dropdown, checkbox or picker", () => {
  const app = Object.entries(FILES).filter(([file]) => !file.includes("/components/ui/") && !file.endsWith(".test.tsx"))
  expect(app.length).toBeGreaterThan(3)
  expect(app.filter(([, text]) => NATIVE.test(text)).map(([file]) => file)).toEqual([])
})
