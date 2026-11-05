import { useEffect, useState } from "react"

export type Theme = "light" | "dark" | "system"

const KEY = "career-lens-theme"
const DARK = "(prefers-color-scheme: dark)"

function saved(): Theme {
  try {
    const value = localStorage.getItem(KEY)
    return value === "light" || value === "dark" ? value : "system"
  } catch {
    return "system"
  }
}

/** The theme of the app. System follows the browser. Dark is the first design. */
export function useTheme() {
  const [theme, setTheme] = useState<Theme>(saved)
  const [systemDark, setSystemDark] = useState(() => window.matchMedia(DARK).matches)
  useEffect(() => {
    const query = window.matchMedia(DARK)
    const change = (e: { matches: boolean }) => setSystemDark(e.matches)
    query.addEventListener("change", change)
    return () => query.removeEventListener("change", change)
  }, [])
  const resolved = theme === "system" ? (systemDark ? "dark" : "light") : theme
  useEffect(() => {
    document.documentElement.classList.toggle("dark", resolved === "dark")
  }, [resolved])
  const choose = (next: Theme) => {
    setTheme(next)
    try {
      localStorage.setItem(KEY, next)
    } catch {
      // Private window: the theme lasts until the page closes.
    }
  }
  return { theme, resolved, choose }
}
