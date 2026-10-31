import { MessageSquare } from "lucide-react"
import { postedText } from "@/lib/jobs"
import { Monogram } from "./JobRow"

export const ROLE_CARD = "relative block w-full rounded-lg border bg-card p-2.5 text-left"
export const ROLE_CARD_ACTIVE = "cursor-pointer outline-none transition-colors hover:border-foreground/25 focus-visible:border-violet-500/60 focus-visible:ring-2 focus-visible:ring-violet-500/30"

type Props = {
  company: string
  title: string
  location: string | null
  badge?: React.ReactNode
  posted?: string | null
  noteCount?: number
  closed?: boolean
}

/** The content of a role card on a board: only what helps the user to find the role. */
export function RoleCardBody({ company, title, location, badge, posted, noteCount = 0, closed }: Props) {
  return (
    <>
      <span className="flex items-center gap-2">
        <Monogram company={company} className="size-6 rounded-md text-[9.5px]" />
        <span className="min-w-0 flex-1 truncate text-[12.5px] text-muted-foreground">{company}</span>
        {badge}
      </span>
      <span className="mt-2 line-clamp-2 block text-[13.5px] leading-snug font-medium break-words">{title}</span>
      {location && <span className="mt-1 block truncate text-[12.5px] text-muted-foreground">{location}</span>}
      {(posted || noteCount > 0 || closed) && (
        <span className="mt-2 flex items-center gap-3 text-[12px] text-muted-foreground">
          {posted && <span className="truncate">{postedText(posted)}</span>}
          {noteCount > 0 && <span className="flex items-center gap-1 tabular-nums"><MessageSquare className="size-3" aria-hidden /><span className="sr-only">Notes: </span>{noteCount}</span>}
          {closed && <span className="font-medium">Closed</span>}
        </span>
      )}
    </>
  )
}
