import { useEffect, useState } from "react"

export const NARROW_QUERY = "(max-width: 899px)"

export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches)
  useEffect(() => {
    const list = window.matchMedia(query)
    const change = (e: { matches: boolean }) => setMatches(e.matches)
    setMatches(list.matches)
    list.addEventListener("change", change)
    return () => list.removeEventListener("change", change)
  }, [query])
  return matches
}
