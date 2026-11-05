import { Pause, Play, Radio, RotateCcw } from "lucide-react"
import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from "react"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

export const SPEEDS = [1, 5, 20, 60] as const
/** How long one envelope flies, in ms of real time. */
export const MOVE_MS = 1500

export type Replay = {
  t: number
  playing: boolean
  speed: number
  live: boolean
  /** How long one envelope flies, in ms of goal time. */
  move: number
  play: () => void
  pause: () => void
  seek: (t: number) => void
  goLive: () => void
  nextSpeed: () => void
}

/**
 * The moment that the map shows. Live: the map follows the newest event (the end of the goal). Play: the time moves at
 * the speed. A seek stops the play and leaves live mode.
 */
export function useReplay(start: number, end: number): Replay {
  const [live, setLive] = useState(true)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState<number>(20)
  const [own, setOwnState] = useState(end)
  const ownRef = useRef(end)
  const setOwn = (value: number) => {
    ownRef.current = value
    setOwnState(value)
  }
  const bounds = useRef({ start, end })
  bounds.current = { start, end }

  useEffect(() => {
    if (!playing) return
    let last = performance.now()
    let frame = requestAnimationFrame(function tick(now) {
      const next = Math.min(bounds.current.end, ownRef.current + (now - last) * speed)
      last = now
      ownRef.current = next
      setOwnState(next)
      if (next >= bounds.current.end) setPlaying(false)
      else frame = requestAnimationFrame(tick)
    })
    return () => cancelAnimationFrame(frame)
  }, [playing, speed])

  const t = live ? end : Math.min(end, Math.max(start, own))
  return {
    t,
    playing,
    speed,
    live,
    move: MOVE_MS * (playing ? speed : 1),
    play: () => {
      setOwn(live || t >= end ? start : t)
      setLive(false)
      setPlaying(true)
    },
    pause: () => setPlaying(false),
    seek: (value) => {
      setPlaying(false)
      setLive(false)
      setOwn(value)
    },
    goLive: () => {
      setPlaying(false)
      setLive(true)
    },
    nextSpeed: () => setSpeed((s) => SPEEDS[(SPEEDS.indexOf(s as (typeof SPEEDS)[number]) + 1) % SPEEDS.length]),
  }
}

const clock = (ms: number) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })

/** Play, pause, the speed, live mode and a scrubber. */
export function ReplayBar({ replay, start, end, running }: { replay: Replay; start: number; end: number; running: boolean }) {
  const track = useRef<HTMLDivElement>(null)
  const dragging = useRef(false)
  const span = Math.max(end - start, 1)
  const at = (clientX: number) => {
    const r = track.current?.getBoundingClientRect()
    if (!r || !r.width) return replay.t
    return start + Math.min(1, Math.max(0, (clientX - r.left) / r.width)) * span
  }
  const down = (e: PointerEvent<HTMLDivElement>) => {
    dragging.current = true
    e.currentTarget.setPointerCapture?.(e.pointerId)
    replay.seek(at(e.clientX))
  }
  const move = (e: PointerEvent<HTMLDivElement>) => dragging.current && replay.seek(at(e.clientX))
  const up = () => {
    dragging.current = false
  }
  const key = (e: KeyboardEvent<HTMLDivElement>) => {
    const step = span / 50
    const next = { ArrowLeft: replay.t - step, ArrowRight: replay.t + step, Home: start, End: end }[e.key]
    if (next === undefined) return
    e.preventDefault()
    replay.seek(Math.min(end, Math.max(start, next)))
  }
  const ended = !running && !replay.playing && replay.t >= end && !replay.live
  const pct = ((replay.t - start) / span) * 100

  return (
    <div className="flex shrink-0 items-center gap-2 border-t px-3 py-1.5 text-[12px]">
      <Button variant="outline" size="icon" className="size-7" aria-label={replay.playing ? "Pause" : ended ? "Play again" : "Play"}
        title={replay.playing ? "Pause" : "Play the goal from the start"} onClick={replay.playing ? replay.pause : replay.play}>
        {replay.playing ? <Pause aria-hidden /> : ended ? <RotateCcw aria-hidden /> : <Play aria-hidden />}
      </Button>
      <Button variant="outline" size="sm" className="h-7 w-12 text-[12px] tabular-nums" aria-label={`Speed ${replay.speed} times. Change the speed`}
        title="Change the speed" onClick={replay.nextSpeed}>
        {replay.speed}×
      </Button>
      <span className="w-[5.5rem] shrink-0 text-muted-foreground tabular-nums">{clock(replay.t)}</span>
      <div ref={track} role="slider" tabIndex={0} aria-label="Replay time" aria-valuemin={start} aria-valuemax={end} aria-valuenow={Math.round(replay.t)}
        aria-valuetext={clock(replay.t)} onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={up} onKeyDown={key}
        className="relative h-5 flex-1 cursor-pointer touch-none rounded outline-none focus-visible:ring-2 focus-visible:ring-ring">
        <span aria-hidden className="absolute inset-x-0 top-1/2 h-1 -translate-y-1/2 rounded-full bg-muted" />
        <span aria-hidden className="absolute top-1/2 left-0 h-1 -translate-y-1/2 rounded-full bg-foreground/50" style={{ width: `${pct}%` }} />
        <span aria-hidden className="absolute top-1/2 size-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-background bg-foreground" style={{ left: `${pct}%` }} />
      </div>
      {running && (
        <Button variant={replay.live ? "secondary" : "outline"} size="sm" className={cn("h-7 text-[12px]", replay.live && "text-red-600 dark:text-red-400")}
          aria-pressed={replay.live} title="Follow the newest event" onClick={replay.goLive}>
          <Radio aria-hidden /> Live
        </Button>
      )}
    </div>
  )
}
