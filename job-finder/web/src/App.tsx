import { ChevronLeft, ServerCrash, TriangleAlert } from "lucide-react"
import { useCallback, useEffect, useReducer, useRef, useState } from "react"
import { toast } from "sonner"
import { CommandMenu } from "@/components/CommandMenu"
import { DeleteDialog, type DeleteTarget } from "@/components/DeleteDialog"
import { ChatView } from "@/components/chat/ChatView"
import { Composer } from "@/components/chat/Composer"
import { FileMenu } from "@/components/chat/FileMenu"
import { ModelPicker } from "@/components/chat/ModelPicker"
import { Welcome } from "@/components/chat/Welcome"
import { AllJobs } from "@/components/jobs/AllJobs"
import { JobsTab } from "@/components/jobs/JobsTab"
import { ActivityTab } from "@/components/activity/ActivityTab"
import { PipelinePage } from "@/components/pipeline/PipelinePage"
import { IconRail } from "@/components/shell/IconRail"
import { GoalList, MandateList } from "@/components/shell/ListPane"
import { SignIn } from "@/components/shell/SignIn"
import { StatusBar } from "@/components/shell/StatusBar"
import { Button } from "@/components/ui/button"
import { Toaster } from "@/components/ui/sonner"
import { TooltipProvider } from "@/components/ui/tooltip"
import { NARROW_QUERY, useMediaQuery } from "@/hooks/useMediaQuery"
import { useTheme } from "@/hooks/useTheme"
import {
  type FileInfo, RECONNECT_DELAYS_MS, clearToken, continueGoal, createGoal, deleteFile, getGoal, getModels, getReport, getStatus, listGoals, listMandates, retryGoal,
  readToken, sendMessage, setGoalModel, stopGoal, watchGoal, whenSignedOut,
  type GoalDetail, type GoalSummary, type Mandate, type ModelCatalog, type ModelChoice, type Report, type Status,
} from "@/lib/api"
import { runningAgents } from "@/lib/chat-state"
import { onlyFor, viewsReducer } from "@/lib/goals"
import { parseRoute, routePath, type Route, type Section } from "@/lib/route"
import { cn } from "@/lib/utils"

const REFRESH_MS = 10_000
const errorText = (err: unknown) => (err instanceof Error ? err.message : String(err))
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))
// Events after which the header and the cards can be out of date.
const REFRESH_EVENTS = new Set(["user_message", "agent_started", "agent_finished", "status_changed", "response_finished", "scrape_finished"])

type Summary = Omit<GoalDetail, "events">

export default function App() {
  const [signedIn, setSignedIn] = useState(() => readToken() !== null)
  useEffect(() => whenSignedOut(() => setSignedIn(false)), [])
  if (!signedIn) return <SignIn onDone={() => setSignedIn(true)} />
  return <Dashboard onSignOut={() => { clearToken(); setSignedIn(false) }} />
}

function Dashboard({ onSignOut }: { onSignOut: () => void }) {
  const { theme, resolved, choose } = useTheme()
  const narrow = useMediaQuery(NARROW_QUERY)
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.pathname))
  const [newGoal, setNewGoal] = useState(false) // narrow screens: show the new chat, not the list
  const [goals, setGoals] = useState<GoalSummary[] | null>(null)
  const [mandates, setMandates] = useState<Mandate[]>([])
  const [status, setStatus] = useState<Status | null>(null)
  const [statusRead, setStatusRead] = useState(false)
  const [views, dispatch] = useReducer(viewsReducer, {})
  const [summary, setSummary] = useState<Summary | null>(null)
  const [report, setReport] = useState<Report | null>(null)
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null)
  const [newChoice, setNewChoice] = useState<ModelChoice | null>(null)
  const [input, setInput] = useState("")
  const [attached, setAttached] = useState<FileInfo[]>([])
  const [uploading, setUploading] = useState(false)
  const [commandOpen, setCommandOpen] = useState(false)
  const [toDelete, setToDelete] = useState<DeleteTarget | null>(null)
  const [loginOpen, setLoginOpen] = useState(false)
  const textarea = useRef<HTMLTextAreaElement>(null)
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const openId = useRef<string | null>(null)
  // The chat of a mandate is a goal: its URL names the mandate, and the list of mandates says which goal.
  const goalId = route.goalId ?? mandates.find((m) => m.id === route.mandateId)?.home_goal_id ?? null
  openId.current = goalId

  const go = useCallback((next: Route) => {
    setRoute(next)
    const path = routePath(next)
    if (path !== window.location.pathname) window.history.pushState(null, "", path)
  }, [])

  useEffect(() => {
    const onPop = () => setRoute(parseRoute(window.location.pathname))
    window.addEventListener("popstate", onPop)
    return () => window.removeEventListener("popstate", onPop)
  }, [])

  const refreshList = useCallback(() => {
    listGoals().then(setGoals).catch(() => {})
    listMandates().then(setMandates).catch(() => {})
  }, [])

  useEffect(() => {
    refreshList()
    const timer = setInterval(refreshList, REFRESH_MS)
    return () => clearInterval(timer)
  }, [refreshList])

  useEffect(() => {
    const read = () => getStatus().then(setStatus).catch(() => setStatus(null)).finally(() => setStatusRead(true))
    void read()
    const timer = setInterval(() => void read(), 15_000)
    return () => clearInterval(timer)
  }, [])

  useEffect(() => {
    getModels().then((c) => { setCatalog(c); setNewChoice((now) => now ?? c.default) }).catch(() => {})
  }, [])

  const readGoal = useCallback((id: string) => {
    getGoal(id).then(({ events: _events, ...rest }) => setSummary((now) => (now && now.goal.id !== id ? now : rest))).catch(() => {})
    getReport(id).then(onlyFor(() => openId.current, id, setReport)).catch(() => {})
  }, [])

  // Open a goal: read it, then follow its events and reconnect after the last event seen.
  useEffect(() => {
    if (!goalId) { setSummary(null); setReport(null); return }
    const controller = new AbortController()
    const id = goalId
    setSummary(null)
    setReport(null)
    const soon = () => {
      if (refreshTimer.current) return
      refreshTimer.current = setTimeout(() => { refreshTimer.current = null; readGoal(id) }, 1000)
    }
    void (async () => {
      let last = 0
      try {
        const { events, ...rest } = await getGoal(id)
        if (controller.signal.aborted) return
        setSummary(rest)
        dispatch({ type: "loaded", id, status: rest.goal.status, events })
        last = events.reduce((max, e) => (typeof e.seq === "number" ? Math.max(max, e.seq) : max), 0)
        getReport(id).then(onlyFor(() => openId.current, id, setReport)).catch(() => {})
      } catch (err) {
        if (controller.signal.aborted) return
        toast.error(errorText(err))
        go({ section: "goals", goalId: null, mandateId: null, tab: "chat" })
        return
      }
      let failures = 0
      while (!controller.signal.aborted && failures < RECONNECT_DELAYS_MS.length) {
        try {
          await watchGoal(id, last, (event) => {
            if (typeof event.seq === "number") last = event.seq
            failures = 0
            if (event.type === "goal_deleted") { controller.abort(); return }
            dispatch({ type: "event", id, event })
            if (REFRESH_EVENTS.has(event.type) || event.saved) soon()
            if (event.type === "status_changed" || (event.type === "notice" && String(event.trigger ?? "").startsWith("mandate_"))) refreshList()
          }, controller.signal)
        } catch {
          if (controller.signal.aborted) return
        }
        if (!controller.signal.aborted) {
          await sleep(RECONNECT_DELAYS_MS[failures])
          failures += 1
        }
      }
      if (!controller.signal.aborted) toast.error("Lost the connection to the server. Open this goal again to catch up.")
    })()
    return () => {
      controller.abort()
      if (refreshTimer.current) clearTimeout(refreshTimer.current)
      refreshTimer.current = null
    }
  }, [goalId, go, readGoal, refreshList])

  // While a goal works, the time and the wait reasons change with no event.
  const working = summary?.goal.status === "running" || summary?.goal.status === "waiting"
  useEffect(() => {
    if (!goalId || !working) return
    const timer = setInterval(() => readGoal(goalId), REFRESH_MS)
    return () => clearInterval(timer)
  }, [goalId, working, readGoal])

  useEffect(() => {
    const title = goals?.find((g) => g.id === goalId)?.title
    document.title = title ? `${title} - Job Finder` : "Job Finder"
  }, [goals, goalId])

  const openGoal = useCallback((id: string, tab: Route["tab"] = "chat") => {
    setInput("")
    go({ section: "goals", goalId: id, mandateId: null, tab })
  }, [go])
  const startNew = useCallback(() => {
    setInput("")
    setNewGoal(true)
    go({ section: "goals", goalId: null, mandateId: null, tab: "chat" })
    requestAnimationFrame(() => textarea.current?.focus())
  }, [go])
  const openSection = (section: Section) => {
    setNewGoal(false)
    go({ section, goalId: null, mandateId: null, tab: "chat" })
  }
  const login = { open: loginOpen, onOpenChange: setLoginOpen, status, onChanged: () => void getStatus().then(setStatus).catch(() => {}) }

  const view = goalId ? views[goalId] ?? null : null
  const running = view ? runningAgents(view) : []
  const busy = Boolean(summary?.busy) || running.length > 0
  const thinking = Boolean(summary?.busy) && view?.status === "running"

  async function send(text: string) {
    const trimmed = text.trim()
    if (!trimmed) return
    const files = attached
    const fileIds = files.map((file) => file.id)
    setInput("")
    setAttached([])
    try {
      if (goalId) await sendMessage(goalId, trimmed, fileIds)
      else {
        const { goal_id } = await createGoal(trimmed, newChoice ?? undefined, fileIds)
        setNewGoal(false)
        openGoal(goal_id)
      }
      refreshList()
    } catch (err) {
      setInput(trimmed)
      setAttached(files)
      toast.error(errorText(err))
    }
  }

  async function control(action: (id: string) => Promise<unknown>) {
    if (!goalId) return
    try {
      await action(goalId)
      refreshList()
      readGoal(goalId)
    } catch (err) {
      toast.error(errorText(err))
    }
  }

  const choice: ModelChoice | null = !catalog ? null : goalId
    ? (summary ? { model: summary.goal.model ?? catalog.default.model, effort: summary.goal.effort ?? catalog.default.effort } : null)
    : newChoice
  async function chooseModel(next: ModelChoice) {
    if (!goalId) { setNewChoice(next); return }
    const before = summary
    setSummary((s) => (s ? { ...s, goal: { ...s.goal, ...next } } : s))
    try {
      await setGoalModel(goalId, next)
    } catch (err) {
      setSummary(before)
      toast.error(errorText(err))
    }
  }

  const composer = (
    <Composer value={input} onChange={setInput} onSubmit={(text) => void send(text)} busy={busy}
      placeholder={goalId ? undefined : "Say what roles you want. Upload your resume, or paste it here."} textareaRef={textarea}
      attached={attached} uploading={uploading}
      onRemove={(id) => {
        setAttached((files) => files.filter((file) => file.id !== id))
        deleteFile(id).catch((err) => toast.error(errorText(err)))
      }}
      tools={<>
        <FileMenu goalId={goalId} onAttach={(file) => setAttached((files) => [...files, file])} onBusy={setUploading} />
        {catalog && choice ? <ModelPicker catalog={catalog} choice={choice} onChange={(c) => void chooseModel(c)}
          locked={goalId && busy ? "Stop the work to change the model or the effort." : undefined} /> : null}
      </>} />
  )

  const goal = summary?.goal
  const homeChat = Boolean(goal?.home_of)
  // A goal of a mandate and the chat of a mandate are in the list of mandates, not in the list of chats.
  const openRow = goals?.find((g) => g.id === goalId)
  const section = route.section === "goals" && (openRow?.mandate_id || openRow?.home_of) ? "mandates" : route.section
  const showList = route.section !== "jobs" && route.section !== "pipeline" && (!narrow || (!goalId && !route.mandateId && !newGoal && (route.section === "mandates" || Boolean(goals?.length))))
  const showMain = !narrow || !showList
  const lensDown = statusRead && (!status || (status.career_lens && !["online", "checking", "not_configured"].includes(status.career_lens.state)))
  const wait = report?.waits.flatMap((w) => (w.reason ? [w.reason] : []))[0] ?? null

  const list = section === "mandates"
    ? <MandateList mandates={mandates} goals={goals ?? []} activeGoalId={goalId} full={narrow}
        onOpen={(id) => go({ section: "mandates", goalId: null, mandateId: id, tab: "chat" })} onOpenGoal={(id) => openGoal(id)}
        onNew={startNew} onDelete={(m) => setToDelete({ kind: "mandate", id: m.id, title: m.title })}
        onDeleteGoal={(g) => setToDelete({ kind: "goal", id: g.id, title: g.title })} />
    : <GoalList goals={goals ?? []} activeId={goalId} full={narrow} onOpen={(id) => openGoal(id)} onNew={startNew}
        onDelete={(g) => setToDelete({ kind: "goal", id: g.id, title: g.title })} />

  const goalScreen = goalId && (
    <>
      <header className="flex h-11 shrink-0 items-center gap-2 border-b px-3">
        {narrow && <Button variant="ghost" size="icon" className="size-7" aria-label="Back to the list" onClick={() => openSection("goals")}><ChevronLeft /></Button>}
        <h2 className="min-w-0 flex-1 truncate font-medium">{route.tab === "jobs" && !homeChat ? "" : report?.title ?? goal?.title ?? "Goal"}</h2>
        {!homeChat && (
          <div role="tablist" aria-label="Goal view" className="flex shrink-0 gap-0.5 rounded-lg bg-muted p-0.5">
            {(["chat", "jobs", "activity"] as const).map((tab) => (
              <button key={tab} role="tab" type="button" aria-selected={route.tab === tab} onClick={() => go({ ...route, tab, step: null })}
                className={cn("h-6 rounded-md px-2.5 text-[12px] capitalize outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  route.tab === tab ? "bg-background font-medium shadow-sm" : "text-muted-foreground hover:text-foreground")}>
                {tab}
              </button>
            ))}
          </div>
        )}
      </header>
      {route.tab === "activity" && !homeChat
        ? <ActivityTab goalId={goalId} events={view?.events ?? []} status={view?.status ?? goal?.status ?? "done"} initialStep={route.step ?? null} />
        : route.tab === "jobs" && !homeChat
        ? <JobsTab report={report} tasks={summary?.tasks.map((t) => t.name) ?? []} onRetry={() => void control(retryGoal)} onChat={() => go({ ...route, tab: "chat" })} />
        : <ChatView view={view} reason={goal?.status_reason ?? null} thinking={thinking} readOnly={Boolean(goal?.agent)} timeLimit={summary?.time_limit ?? null}
            onChoose={(text) => void send(text)} onStop={() => void control(stopGoal)} onRetry={() => void control(retryGoal)} onContinue={() => void control(continueGoal)}
            onActivity={homeChat ? undefined : (step) => go({ ...route, tab: "activity", step })} composer={composer} />}
    </>
  )

  return (
    <TooltipProvider delayDuration={200}>
      <div className="app-bg flex h-dvh flex-col overflow-hidden text-[13px] text-foreground">
        <div className="flex min-h-0 flex-1">
          {!narrow && <IconRail onSignOut={onSignOut} section={section} onSection={openSection} theme={theme} resolved={resolved} onTheme={choose} narrow={false} login={login} />}
          {showList && list}
          {showMain && (
            <main className="flex min-h-0 min-w-0 flex-1 flex-col">
              {status && !status.anthropic_configured && (
                <p role="alert" className="flex items-center gap-2 bg-red-500/10 px-3.5 py-1.5 text-red-800 dark:text-red-200">
                  <TriangleAlert className="size-3.5 shrink-0" aria-hidden />
                  Claude is not connected. The agents cannot start.
                  <button type="button" onClick={() => setLoginOpen(true)} className="font-medium underline underline-offset-2">Connect Claude</button>
                </p>
              )}
              {lensDown && (
                <p role="alert" className="flex items-center gap-2 bg-red-500/10 px-3.5 py-1.5 text-red-800 dark:text-red-200">
                  {status ? <TriangleAlert className="size-3.5 shrink-0" aria-hidden /> : <ServerCrash className="size-3.5 shrink-0" aria-hidden />}
                  {status ? "career-lens does not answer. The agents cannot search or read pages. Goals wait until it is back." : "The job-finder server does not answer. Start it, then this page continues."}
                </p>
              )}
              {route.section === "jobs" ? <AllJobs openJob={route.job ?? null} onOpenJob={(job) => go({ section: "jobs", goalId: null, mandateId: null, tab: "chat", job: { goalId: job.goal_id, jobId: job.id } })} onBoard={() => openSection("jobs")} onShowInPipeline={(highlight) => go({ section: "pipeline", goalId: null, mandateId: null, tab: "chat", highlight })} onNewGoal={startNew} onOpenGoal={(id) => openGoal(id, "jobs")} />
                : route.section === "pipeline" ? <PipelinePage roleKey={route.roleKey ?? null} highlight={route.highlight ?? null} onOpenRole={(roleKey) => go({ section: "pipeline", goalId: null, mandateId: null, tab: "chat", roleKey })} onBoard={() => openSection("pipeline")} onOpenGoal={(id) => openGoal(id, "jobs")} onJobs={() => openSection("jobs")} />
                : goalId ? goalScreen
                : route.mandateId ? <p className="p-4 text-muted-foreground">Reading the mandate</p>
                : route.section === "mandates" ? <p className="m-auto max-w-[320px] p-4 text-center text-muted-foreground">Open a mandate to see its chat and its goals.</p>
                : <Welcome composer={composer} hasGoals={Boolean(goals?.length)} onDraft={(text) => { setInput(text); requestAnimationFrame(() => textarea.current?.focus()) }} />}
            </main>
          )}
        </div>
        {narrow && <IconRail onSignOut={onSignOut} section={section} onSection={openSection} theme={theme} resolved={resolved} onTheme={choose} narrow login={login} />}
        <StatusBar onLogin={() => setLoginOpen(true)} status={status} wait={wait} spend={report ? { used: report.goal.spend_usd, max: report.goal.max_spend_usd } : null}
          running={goals?.filter((g) => g.status === "running").length ?? 0} onCommand={() => setCommandOpen(true)} />
      </div>
      <CommandMenu open={commandOpen} onOpenChange={setCommandOpen} onSettings={() => setLoginOpen(true)} goals={goals ?? []} mandates={mandates} dark={resolved === "dark"}
        onNew={startNew} onJobs={() => openSection("jobs")} onPipeline={() => openSection("pipeline")} onGoal={(id) => openGoal(id)}
        onMandate={(id) => go({ section: "mandates", goalId: null, mandateId: id, tab: "chat" })}
        onToggleTheme={() => choose(resolved === "dark" ? "light" : "dark")} />
      <DeleteDialog target={toDelete} onClose={() => setToDelete(null)} onDeleted={(target) => {
        setToDelete(null)
        if (target.id === goalId || target.id === route.mandateId) openSection(route.section)
        refreshList()
      }} />
      <Toaster position="top-center" />
    </TooltipProvider>
  )
}
