import type { Schedule } from "./api"

const SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

export const blankShift = (): Schedule => ({
  enabled: true,
  timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
  start_jitter_minutes: 15,
  end_jitter_minutes: 20,
  days: SHORT.map((_, i) => ({
    weekday: i + 1, working: i < 5, start_local: i < 5 ? "10:00" : null, end_local: i < 5 ? "19:00" : null,
    lunch_minutes: i < 5 ? 45 : 0, break_count: 0, break_minutes: 0,
  })),
})

/** One line for a shift. Days with the same hours stay together. */
export function shiftText(schedule: Schedule | null): string {
  if (!schedule) return "No shift. This account does not scrape."
  if (!schedule.enabled) return "The shift is off."
  const working = schedule.days.filter((day) => day.working)
  if (!working.length) return "No working days."
  const hours = new Map<string, string[]>()
  for (const day of working) {
    const key = `${day.start_local?.slice(0, 5)} to ${day.end_local?.slice(0, 5)}`
    hours.set(key, [...(hours.get(key) ?? []), SHORT[day.weekday - 1]])
  }
  return `${[...hours].map(([key, days]) => `${days.join(", ")} ${key}`).join("; ")} (${schedule.timezone})`
}

/** The body of the shift route. A day off has no hours. */
export function shiftBody(schedule: Schedule) {
  return {
    ...schedule,
    days: schedule.days.map((day) => (day.working ? day : { ...day, lunch_minutes: 0, break_count: 0, break_minutes: 0 })),
  }
}
