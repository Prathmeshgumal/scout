# job-finder

job-finder is a chat agent that finds jobs for one person. Tell the agent what you want, for example
"Find senior backend roles in Bengaluru or remote India, Python or Go, 20 fit roles each weekday."

The main agent talks with you and gives the work to subagents. All subagents use one shared store in Postgres.

| Subagent | Work |
|---|---|
| `candidate-profile` | Make the candidate profile from your words and your resume text |
| `job-discovery` | Find roles with Google and LinkedIn search |
| `job-research` | Read the full job description and the company facts |
| `fit-check` | Compare each role with the profile: fit, partial, not_fit or unsure |
| `application-prep` | Write an application note for each fit role |

job-finder gets all web data from [career-lens](../career-lens-v2). It does not use the built-in `WebSearch` and
`WebFetch` tools.

## You need

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- Node.js 20 or newer, for the web app
- Docker, for Postgres
- A Claude login: a subscription token or an API key
- A running career-lens stack and a customer key (`cl_v1.`)

## Set up

```bash
uv sync
cp .env.example .env                 # set the career-lens settings
uv run job-finder login              # save a Claude login
docker compose up -d                 # start Postgres and MinIO
cd web && npm install && npm run build && cd ..
uv run job-finder serve              # then open http://127.0.0.1:8000
```

The app applies its database migrations when it starts.

## Claude login

| Login | Setting | How to get it |
|---|---|---|
| Subscription token | `CLAUDE_CODE_OAUTH_TOKEN` | Run `uv run job-finder login` |
| API key | `ANTHROPIC_API_KEY` | Run `uv run job-finder login --api-key` |

You can also do this in the web app. Click the **Settings** button at the bottom of the rail. Choose **Claude Code** or **API key**.

- **Claude Code:** click **Use Claude Code**, authorize, and paste the code. Click **Use another account** to change the account.
- **API key:** paste the key.

The login that you choose in the web app is the login in use. Only the computer that runs job-finder can change the login.

- With no choice, the API key wins when both are set.
- In Docker Compose, the app restarts after a login change. Running goals continue after the restart.
- Outside Docker, restart `job-finder serve` after a login change.

## Use the web app

| Page | Use |
|---|---|
| Chat | Talk with the main agent. Stop, retry or continue a goal. Select the model and the effort. |
| Jobs tab of a goal | See the progress, the roles, the verdicts and the application notes. Download the report. |
| All jobs | See the roles of all goals. |
| Mandates | See the work that repeats on a schedule. |

A role shows only the facts that the source gave. A role that has no full job description says so.

To work on the UI, run `uv run job-finder serve`. Then run `npm run dev` in `web`.

## Upload files

Use the paperclip in the message box to attach a file to a message. Use the same menu to upload your resume.

- Agents read text files, PDF files, Word files and images. The largest file is 10 MB.
- A scanned PDF has no text. Paste the text into the chat.
- You can store other file types and download them. Agents cannot read them.
- One resume applies to all goals. A new resume replaces the old one.
- Only the candidate-profile, fit-check and application-prep subagents get the resume text.
- The text and the images that agents read go to Claude.

MinIO keeps the files. Postgres keeps the facts of each file and its text.

## Track your applications

Open **Jobs** to see the roles of each goal. Click a goal to show its roles. Click **Show all jobs** to see the roles of all goals at once.

Open **Job pipeline** to track your applications. The page has four stages:

| Stage | Meaning |
|---|---|
| Fit jobs | An agent marked the role as a fit, or you added the role. |
| Looking for referral | You look for a person who can refer you. |
| Applied | You applied to the role. |
| Responded | The company responded. |

- Choose the stage of a role yourself. The agents do not change it.
- Click a role to add notes.
- The funnel counts the roles that reached each stage or a later stage.
- A role and its notes stay in the pipeline when you delete its goal.

## Use the command line

```bash
uv run job-finder chat               # start a new goal
uv run job-finder chat 3f9a2c1e      # open a goal again
uv run job-finder goals              # list the goals
uv run job-finder show 3f9a2c1e      # also --jobs, --notes, --research or --events
uv run job-finder delete 3f9a2c1e    # asks first, or use --yes
```

In a chat, use `/status`, `/jobs`, `/stop` and `/retry`.

## Mandates

A mandate is work that repeats on a schedule. To make one, tell the agent in a chat, for example "every weekday at
09:00". The agent shows all the settings and waits for your yes. Then that chat manages the mandate.

Change, pause, resume or end a mandate in its chat. A mandate needs a ready candidate profile.

```bash
uv run job-finder mandate list
uv run job-finder mandate show 3f9a2c1e
uv run job-finder mandate delete 3f9a2c1e
```

## Limits

| Limit | Setting | Default |
|---|---|---|
| Spend for each goal | `JOB_FINDER_MAX_SPEND_USD` | $2.00 |
| Fresh scrapes for each goal | `JOB_FINDER_MAX_FRESH_SCRAPES` | 15 |
| Total spend of a mandate | `JOB_FINDER_MANDATE_TOTAL_USD` | $10.00 |
| Time limit for each mandate goal | `JOB_FINDER_MANDATE_TIME_LIMIT_MINUTES` | 120 |

`.env.example` shows all the settings.

## Work on a subagent

Each subagent is a folder in `job_finder/agents/`, with `agent.py` and `prompt.md`. Run one subagent alone:

```bash
uv run job-finder agent job-discovery "Find 5 Go roles in Pune"
uv run job-finder agent job-research "Research the roles" --jobs-from <goal id>
```

## Test

```bash
docker compose up -d                 # the tests use a real Postgres
uv run pytest
uv run ruff check .
cd web && npm test && npm run build
```

The tests do not contact LinkedIn, Google or another live site.
