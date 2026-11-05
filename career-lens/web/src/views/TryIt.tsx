import { useState } from "react"
import { Field, Page, Problem, fieldClass } from "@/components/Status"
import { Button } from "@/components/ui/button"
import { Select } from "@/components/ui/select"
import { tryRequest } from "@/lib/api"

const CHOICES = [
  { id: "google-search", label: "Google search", field: "query", hint: "go engineer jobs pune" },
  { id: "linkedin-company", label: "LinkedIn company", field: "url", hint: "https://www.linkedin.com/company/example/" },
  { id: "linkedin-individual", label: "LinkedIn person", field: "url", hint: "https://www.linkedin.com/in/example/" },
  { id: "linkedin-job", label: "LinkedIn job posting", field: "url", hint: "https://www.linkedin.com/jobs/view/123/" },
  { id: "linkedin-job-search", label: "LinkedIn job search", field: "keywords", hint: "go engineer" },
  { id: "url", label: "Single page", field: "url", hint: "https://example.com/" },
]

export function TryIt({ onOpen }: { onOpen: (id: string) => void }) {
  const [path, setPath] = useState(CHOICES[0].id)
  const [value, setValue] = useState("")
  const [error, setError] = useState<string | null>(null)
  const choice = CHOICES.find((item) => item.id === path) ?? CHOICES[0]
  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    try {
      const { request_id } = await tryRequest(path, { [choice.field]: value.trim() })
      onOpen(request_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : "The request failed.")
    }
  }
  return (
    <Page title="Try it" note="Send one request and watch it run. It uses the limits of the accounts.">
      <form onSubmit={submit} className="flex max-w-xl flex-col gap-3">
        <Problem text={error} />
        <Field label="Type">
          <Select label="Type" value={path} onChange={setPath} className="w-full" choices={CHOICES.map((item) => ({ value: item.id, label: item.label }))} />
        </Field>
        <Field label={choice.field === "url" ? "URL" : "Search words"}>
          <input required placeholder={choice.hint} value={value} onChange={(e) => setValue(e.target.value)} className={fieldClass} />
        </Field>
        <Button type="submit" className="self-start" disabled={!value.trim()}>Send the request</Button>
      </form>
    </Page>
  )
}
