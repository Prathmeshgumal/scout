import { useCallback, useEffect, useRef, useState } from "react"

/** Read data now and each `ms`. A stale reply never replaces a newer one. It stops when `ms` is null. */
export function usePoll<T>(read: () => Promise<T>, ms: number | null, key: string = "") {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const latest = useRef(read)
  latest.current = read
  const turn = useRef(0)

  const refresh = useCallback(async () => {
    const mine = ++turn.current
    try {
      const value = await latest.current()
      if (mine === turn.current) { setData(value); setError(null) }
    } catch (err) {
      if (mine === turn.current) setError(err instanceof Error ? err.message : "The server did not answer.")
    }
  }, [])

  useEffect(() => { setData(null); void refresh() }, [key, refresh])
  useEffect(() => {
    if (ms === null) return
    const timer = setInterval(() => { if (!document.hidden) void refresh() }, ms)
    return () => clearInterval(timer)
  }, [ms, refresh])
  return { data, error, refresh }
}
