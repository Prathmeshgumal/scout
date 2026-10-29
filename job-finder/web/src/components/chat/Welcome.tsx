import type { ReactNode } from "react"

const EXAMPLES = [
  "Find 5 backend roles in Bengaluru, hybrid, posted this week. Here is my resume:",
  "I am a data analyst with 3 years of SQL and Python. Find remote roles in India.",
  "Find 10 roles each weekday morning for a senior Go engineer in Pune.",
]

export function Welcome({ composer, onDraft, hasGoals }: { composer: ReactNode; onDraft: (text: string) => void; hasGoals: boolean }) {
  return (
    <div className="flex min-h-0 flex-1 flex-col justify-center overflow-y-auto px-4 py-6">
      <div className="mx-auto flex w-full max-w-[640px] flex-col gap-4">
        <div>
          <h1 className="text-[22px] leading-tight font-semibold">{hasGoals ? "What roles do you want next?" : "Tell the agent what job you want"}</h1>
          <p className="mt-1.5 text-muted-foreground">
            Say what you do and what you want, and paste your resume as text. The agents find roles, read each job, and tell you which ones fit and why. They never apply for you.
          </p>
        </div>
        {composer}
        <div className="flex flex-col items-start gap-1">
          {EXAMPLES.map((text) => (
            <button key={text} type="button" onClick={() => onDraft(text)} className="max-w-full rounded px-1 py-0.5 text-left text-muted-foreground hover:text-foreground">{text}</button>
          ))}
        </div>
      </div>
    </div>
  )
}
