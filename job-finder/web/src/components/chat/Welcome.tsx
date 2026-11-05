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
          <h1 className="text-[22px] leading-tight font-semibold">{hasGoals ? "What job do you want to find next?" : "What job do you want to find?"}</h1>
          <p className="mt-1.5 text-muted-foreground">
            Tell me about the job you want and add your resume. Tell me what matters to you too, such as the city, the pay, remote work or the company size. Then I find the roles that fit you best.
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
