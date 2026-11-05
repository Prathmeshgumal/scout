# jobscout

jobscout finds jobs. It holds two apps and the files that run them together.

| Folder | What it does |
|---|---|
| `career-lens/` | Scrapes LinkedIn, Google and web pages. It has an API, a console and a native worker. |
| `job-finder/` | The agent and its web app. It gets all web data from career-lens. |

`docker-compose.yml` runs the whole stack. Read the top of that file for the commands.

- job-finder: http://127.0.0.1:8000
- career-lens console: http://127.0.0.1:8090/console

The native worker is not a container. It is the systemd user service `career-lens-worker`.

Each app has its own `AGENTS.md`, `pyproject.toml` and tests. Run the checks from the folder of the app.
