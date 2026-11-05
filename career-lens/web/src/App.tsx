import { useCallback, useEffect, useState } from "react"
import { IconRail } from "@/components/IconRail"
import { Toaster } from "@/components/ui/sonner"
import { TooltipProvider } from "@/components/ui/tooltip"
import { usePoll } from "@/hooks/usePoll"
import { NARROW_QUERY, useMediaQuery } from "@/hooks/useMediaQuery"
import { useTheme } from "@/hooks/useTheme"
import { overview, session, signOut, whenSignedOut } from "@/lib/api"
import { type Route, type Section, parseRoute, routePath } from "@/lib/route"
import { Accounts } from "@/views/Accounts"
import { Login } from "@/views/Login"
import { Overview } from "@/views/Overview"
import { RequestDetailView } from "@/views/RequestDetailView"
import { Requests } from "@/views/Requests"
import { TryIt } from "@/views/TryIt"
import { Workers } from "@/views/Workers"

export default function App() {
  const [signedIn, setSignedIn] = useState<boolean | null>(null)
  useEffect(() => {
    whenSignedOut(() => setSignedIn(false))
    session().then((body) => setSignedIn(body.signed_in)).catch(() => setSignedIn(false))
  }, [])
  const theme = useTheme()
  return (
    <TooltipProvider>
      {signedIn === true && <Console theme={theme} onSignOut={() => setSignedIn(false)} />}
      {signedIn === false && <Login onDone={() => setSignedIn(true)} />}
      <Toaster />
    </TooltipProvider>
  )
}

function Console({ theme, onSignOut }: { theme: ReturnType<typeof useTheme>; onSignOut: () => void }) {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.pathname))
  const narrow = useMediaQuery(NARROW_QUERY)
  const go = useCallback((next: Route) => {
    window.history.pushState(null, "", routePath(next))
    setRoute(next)
  }, [])
  useEffect(() => {
    const change = () => setRoute(parseRoute(window.location.pathname))
    window.addEventListener("popstate", change)
    return () => window.removeEventListener("popstate", change)
  }, [])

  const shared = route.section === "overview" || route.section === "workers" || route.section === "accounts"
  const { data, error, refresh } = usePoll(overview, shared ? 5000 : null)
  const openRequest = (id: string) => go({ section: "requests", requestId: id })
  const section = (id: Section) => go({ section: id, requestId: null })

  return (
    <div className={narrow ? "flex h-dvh flex-col-reverse bg-background" : "flex h-dvh bg-background"}>
      <IconRail section={route.section} onSection={section} theme={theme.theme} resolved={theme.resolved} onTheme={theme.choose}
        onSignOut={() => { void signOut().finally(onSignOut) }} narrow={narrow} />
      <main className="min-w-0 flex-1 overflow-y-auto">
        {route.section === "overview" && <Overview data={data} error={error} onOpen={openRequest} onSeeAll={() => section("requests")} onWorkers={() => section("workers")} />}
        {route.section === "requests" && (route.requestId
          ? <RequestDetailView key={route.requestId} id={route.requestId} onBack={() => section("requests")} />
          : <Requests onOpen={openRequest} />)}
        {route.section === "workers" && <Workers data={data} error={error} onChange={() => void refresh()} />}
        {route.section === "accounts" && <Accounts data={data} error={error} onChange={() => void refresh()} />}
        {route.section === "try" && <TryIt onOpen={openRequest} />}
      </main>
    </div>
  )
}
