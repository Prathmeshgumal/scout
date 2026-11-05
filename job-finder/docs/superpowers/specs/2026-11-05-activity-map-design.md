# Activity map: design

The mockup that the maintainer approved is the reference: `map.html` (system map, Pixel and Blueprint, light and
dark). This document says how to build it from real data.

## Intent

The Activity tab shows a list of steps and agent lanes. It does not show why a goal is slow, what career-lens does,
or how the parts of the system work together. The map shows the whole system while a goal runs, and replays a goal
that ended. A user understands the product by watching it.

## Goals

- A **Map** view in the Activity tab, next to the step detail. It shows three areas:
  - **job-finder**: Scout (main agent), Hunter (job-discovery), Sage (job-research), Judge (fit-check),
    Penn (application-prep), Claude, the Reader (small model), the job board, the spend jar, the Gate and the Archive.
  - **career-lens**: the Dispatcher, the Queue with tickets and a live wait, the Chrome workers, the History.
  - **The web**: Google, LinkedIn, careers sites.
- Moving envelopes for each request and result: between agents (a subagent run and its result), and from an agent
  through the Gate to career-lens and back (each career-lens request).
- Live while the goal runs (the event stream), and replay for any goal: play, pause, speed, and a scrubber. A click on
  a step moves the replay to that step.
- **Pixel** and **Blueprint** styles of the same map. Light and dark mode for both.
- A click on a part of the map shows what it is and why it exists.
- A "Requests and results" list with the real tool calls and their results.
- A queue-wait lane under the agent lanes.

## Non-goals

- No change to career-lens.
- No new data from career-lens that its API does not give now. The map does not name a worker or an account: the API
  does not say which worker ran a request.
- No sound. No characters that walk on paths: things move along straight lines.

## Anti-goals

- **Do not invent.** Each thing on the map comes from an event. If the data is missing, the map shows less. It does
  not guess. The mockup's case buttons were demos; the real map has no demo data.
- The map must not slow the tab: a goal can have 500 events.

## The data

| Map part | Source |
|---|---|
| Agent working, its bubble | `tool_started`/`tool_finished` (agent, label); `agent_started`/`agent_finished` |
| Envelopes between agents | `agent_started` (task: the request) and `agent_finished` (summary: the result) |
| Claude thinking | an agent has a step or a run open, and no career-lens request of that agent waits |
| Spend jar | `response_finished.spend_usd`; the cap is the goal's `max_spend_usd` |
| Job board | `tool_finished.saved` of `save_jobs` ("N new roles"), `save_job_research`, `save_fit` (label "Saving a verdict: fit") |
| Gate refusals | `site_blocked` |
| A career-lens request: submit, queue, run, done, from the history | **new** `lens_request` events (below), and the existing `scrape_queued`/`scrape_status`/`scrape_finished` for background scrapes |
| Reader working | a `read_page` step after its `lens_request` finished and before its `tool_finished` |
| LinkedIn | "in use" while a LinkedIn request is open; a wait reason (for example outside the shift) on its ticket; else "not used in this goal" |
| History | the count of requests in this goal that career-lens answered from its history (`satisfied`) |

### New event: `lens_request`

`google_search` and `read_page` wait for career-lens in the tool. Today they record nothing about the wait. Record
one event at each change, in `tools/career_lens.py`:

```json
{"request_id": "rq-…", "tool": "read_page", "target": "https://jobs.lever.co/…", "agent": "job-research",
 "status": "queued", "wait_reason": "pacing", "next_attempt_at": "2026-11-05T10:39:02Z"}
```

- `status`: the career-lens status (`queued`, `assigned`, `running`, `satisfied`, `succeeded`, `failed`,
  `cancelled`). The first event is at submit. The last event has the final status.
- At the end, add `requested_at`, `started_at` and `finished_at` from career-lens: the real queue time and work time.
- `read_page` polls the request status only while the result is 202, at the same interval as now: one more small
  GET for each poll.
- The agent name: the tool context does not know it. Match the event to its step in the web app: the same tool,
  the same target, the first open step. Record `target` exactly as the step label shows it.

## The look

The approved mockup, with these changes:

- In the real tab, the map takes the place of the step detail pane, with a **Map / Step** switch. The step list and
  the lanes stay. The choice is kept in local storage, like the tree and the lane height.
- Workers: one browser window for each career-lens request that runs now, maximum 3 shown, then "+N".
- The Queue: one ticket for each queued request: tool, short target, and the wait ("wait 0:37" from
  `next_attempt_at`, or the wait reason in words).
- Names: Scout, Hunter, Sage, Judge, Penn. Other agents (a new subagent type) get a generic character with the
  agent name.
- The explain texts of the mockup, in ASD-STE100.

## Decisions for the builder

- Plain React components and CSS (Tailwind plus a small CSS file for the pixel art). No canvas and no game library.
- A pure function `mapState(events, t)` gives the state of the map at time t. The replay, the live view and the tests
  all use it.
- Movement: an envelope moves for a fixed time (1.5 s at 1× speed) from the event time. Positions come from the state
  function, so the scrubber works.
- No native form controls (`AGENTS.md`).

## Left to the builder

- The exact pixel art and the positions, close to the mockup.
- How the map scales to the pane: the map has a fixed design size (1000 × 600) and scales to fit.

## Calls made for the maintainer (check these)

- **Live and replay:** while the goal runs, the map follows the newest event. A drag of the scrubber or a click on
  play leaves live mode. A **Live** button goes back.
- **Background scrapes** (`scrape_queued`, for LinkedIn and company scrapes) have no agent name. The envelope comes
  from the agent of the newest open step whose tool queued it (`search_linkedin_jobs`, `scrape_linkedin_job`,
  `scrape_company`).
- **Theme:** the map uses the app's light and dark theme. It has no theme switch of its own.
- **Default view:** the Map view is the default for a goal with career-lens requests. Else the step detail is the
  default. The user's last choice wins.
