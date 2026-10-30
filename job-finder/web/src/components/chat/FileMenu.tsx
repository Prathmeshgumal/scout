import { Download, FileUp, Paperclip, Trash2, Upload } from "lucide-react"
import { useRef, useState } from "react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { type FileInfo, deleteFile, fileUrl, listFiles, uploadFile } from "@/lib/api"

type Props = {
  goalId: string | null
  /** A new attachment. The next message takes it. */
  onAttach: (file: FileInfo) => void
  onBusy: (busy: boolean) => void
}

const ACCEPT = ".pdf,.docx,.md,.txt,.csv,.json,.yaml,.yml,.html,.png,.jpg,.jpeg,.gif,.webp"
const errorOf = (err: unknown) => (err instanceof Error ? err.message : "The upload failed.")

/** Attach a file to the next message, and keep the one resume that all goals use. */
export function FileMenu({ goalId, onAttach, onBusy }: Props) {
  const attachInput = useRef<HTMLInputElement>(null)
  const resumeInput = useRef<HTMLInputElement>(null)
  const [resume, setResume] = useState<FileInfo | null>(null)
  const [files, setFiles] = useState<FileInfo[]>([])

  const refresh = () => listFiles(goalId).then((list) => {
    setResume(list.resume)
    setFiles(list.files)
  }).catch((err) => toast.error(errorOf(err)))

  async function attach(chosen: File[]) {
    onBusy(true)
    for (const file of chosen) {
      try {
        onAttach(await uploadFile(file))
      } catch (err) {
        toast.error(errorOf(err))
      }
    }
    onBusy(false)
  }

  async function saveResume(file: File) {
    onBusy(true)
    try {
      const saved = await uploadFile(file, "resume")
      setResume(saved)
      if (saved.readable) toast.success(`Saved ${saved.name} as your resume.`)
      else toast.warning(`Saved ${saved.name}. It has no text that agents can read. Paste the text in the chat.`)
    } catch (err) {
      toast.error(errorOf(err))
    }
    onBusy(false)
  }

  async function removeResume() {
    if (!resume) return
    try {
      await deleteFile(resume.id)
      setResume(null)
      toast.success("Removed your resume.")
    } catch (err) {
      toast.error(errorOf(err))
    }
  }

  return (
    <>
      <input ref={attachInput} type="file" multiple hidden aria-label="Attach files" onChange={(e) => {
        const chosen = Array.from(e.target.files ?? [])
        e.target.value = ""
        if (chosen.length) void attach(chosen)
      }} />
      <input ref={resumeInput} type="file" hidden accept={ACCEPT} aria-label="Upload resume" onChange={(e) => {
        const file = e.target.files?.[0]
        e.target.value = ""
        if (file) void saveResume(file)
      }} />
      <DropdownMenu onOpenChange={(open) => { if (open) void refresh() }}>
        <DropdownMenuTrigger asChild>
          <Button type="button" variant="ghost" size="icon" className="size-7 rounded-md" aria-label="Files"><Paperclip /></Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-72">
          <DropdownMenuItem onSelect={() => attachInput.current?.click()}><FileUp />Attach files</DropdownMenuItem>
          <DropdownMenuItem onSelect={() => resumeInput.current?.click()}><Upload />{resume ? "Replace your resume" : "Upload your resume"}</DropdownMenuItem>
          {resume && (
            <>
              <DropdownMenuSeparator />
              <DropdownMenuLabel className="truncate font-normal text-muted-foreground">Resume: {resume.name}</DropdownMenuLabel>
              <DropdownMenuItem asChild><a href={fileUrl(resume.id)} download><Download />Download your resume</a></DropdownMenuItem>
              <DropdownMenuItem variant="destructive" onSelect={() => void removeResume()}><Trash2 />Remove your resume</DropdownMenuItem>
            </>
          )}
          {files.length > 0 && (
            <>
              <DropdownMenuSeparator />
              <DropdownMenuLabel className="font-normal text-muted-foreground">Files of this goal</DropdownMenuLabel>
              {files.map((file) => (
                <DropdownMenuItem key={file.id} asChild>
                  <a href={fileUrl(file.id)} download><Download /><span className="truncate">{file.name}</span></a>
                </DropdownMenuItem>
              ))}
            </>
          )}
          <DropdownMenuSeparator />
          <p className="px-2 py-1.5 text-xs text-muted-foreground">Agents read PDF, Word, text and image files up to 10 MB. The text and the images go to Claude.</p>
        </DropdownMenuContent>
      </DropdownMenu>
    </>
  )
}
