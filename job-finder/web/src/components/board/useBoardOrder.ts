import { useCallback, useEffect, useState } from "react"
import { toast } from "sonner"
import { getBoardOrder, saveBoardOrder, type BoardName } from "@/lib/api"
import type { Positions } from "@/lib/board-order"

/** The places that the user gave to the cards of a board. A new order shows at once, before the server answers. */
export function useBoardOrder(board: BoardName) {
  const [positions, setPositions] = useState<Positions>({})
  useEffect(() => {
    let active = true
    getBoardOrder(board).then((body) => { if (active) setPositions(body?.positions ?? {}) }).catch(() => {})
    return () => { active = false }
  }, [board])

  const save = useCallback(async (column: string, keys: string[]) => {
    setPositions((now) => ({ ...now, ...Object.fromEntries(keys.map((key, position) => [key, { column, position }])) }))
    try {
      await saveBoardOrder(board, column, keys)
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "The new order was not saved.")
    }
  }, [board])

  return { positions, save }
}
