# sazed-worker

Runs agent jobs for [Sazed](https://github.com/tharpep/sazed) on an isolated homelab container (CT 110). Research and second opinions run on Antigravity (`agy`, Gemini with Google Search grounding). Code reviews and PR-only code changes run on Claude Code.

Sazed owns the job queue, the prompts, the model allowlist, the daily caps and the approval gate. This service only:
- claims a job,
- runs the agent CLI in a fresh workspace,
- streams the agent's steps back (which also serves as the heartbeat and the cancel check),
- posts the result.

It runs one job at a time and uses only the Python standard library.

## Code layout

`worker.py` is the entry point the systemd unit runs; the code is in the `sazed_worker` package beside it:

| module | what it holds |
|---|---|
| `config` | environment, the agent user's paths, limits, the logger, the stop flag |
| `scrub`, `sazed_api` | secret scrubbing, and the three Sazed worker routes |
| `agent_process` | running children as the agent user, killing their process groups, `JobFailed` |
| `agy`, `claude` | each CLI's command line (and Claude's profiles) and how its event stream is read |
| `run` | one agent process, streamed to Sazed under the timeout and tool-call cap |
| `repo`, `changes`, `checks` | workspaces and checkouts (and the repo's CLAUDE.md/REVIEW.md for the prompt), the changed-file list, the checks on a change job's edits |
| `report` | the shape check on a change job's closing JSON block, and its one repair prompt |
| `jail`, `deps`, `tests` | bubblewrap jail for dependency installs, checks and tests; dependency setup; the repo's tests run by the worker |
| `jobs` | one job from start to its finish payload |
| `main` | the claim loop and workspace cleanup |

## How it runs

- **Users:** `worker.py` runs as root under systemd (`sazed-worker.service`), but only so it can start every agent, and every git command, as the unprivileged `agent` user. The CLIs and their logins live in `/home/agent`.
- **Why a separate user:** an agent can't read the worker's `/proc/<pid>/environ` (worker key, Claude token) or the root-only `/etc/sazed-eco/sazed-worker.env`. A same-user agent could, as a Claude Code review of this repo pointed out.
- **Agent environment:** minimal. Claude Code gets only its own token.
- **Scrubbing:** known secret values are removed from everything posted to Sazed.
- **Review profile:** `--tools=Read,Grep,Glob` only (no Bash: even git diff can write files with `--output=`).
  - It refuses reads of the agent's own logins and `/proc`.
  - `--setting-sources=user` ignores a repo's `.claude/settings.json` hooks.
- **Unit gotcha:** no explicit `User=root` in the unit. With `NoNewPrivileges=yes`, systemd then drops CAP_SETUID and the privilege drop fails.
- **Change profile:**
  - The agent edits a copy of the checkout with no `.git` folder, using `--tools=Read,Grep,Glob,Edit,Write` and edits limited to the working copy (`Edit(./**)`, `Write(./**)`). No shell.
  - **Checks:** before the PR, the worker runs fixed commands as the `agent` user on what the job changed. A finding counts only if the edit introduced it:
    - ruff on changed lines, with the repo's own config
    - `bash -n` on shell scripts
    - `gofmt`, applied to Go files that were formatted before
    - `go vet` (`GOTOOLCHAIN=local`, `GOFLAGS=-mod=readonly`), compared with the untouched checkout
    - `tsc --noEmit`, after `pnpm install --frozen-lockfile --ignore-scripts` (through corepack) or `npm ci --ignore-scripts` in the workspace folder, also compared with the untouched checkout

    Findings go into one fix round. A missing tool or a failed install skips that check and is named in `meta.checks_skipped`. `install-tools.sh` installs Go, Node, npm, corepack, bubblewrap, socat, Poetry, uv, and Pythons 3.12 and 3.13 on CT 110.
  - **Jail:** dependency installs and checks load repo-controlled code before or after the agent, with network. They run as the `agent` user inside bubblewrap: read-only system, only their own workspace writable, the agent's home hidden (`~/.claude`, `~/.gemini`, other jobs), a private `/tmp` and no other processes.
  - **The repo's tests, run by the worker** (no agent shell): pytest for a Poetry project with tests, `go test ./...` for a Go module with tests. Dependencies are installed first, in the jail: `poetry install --no-root` into a workspace virtualenv (Python 3.12 or 3.13 from uv when the system's 3.14 doesn't fit) or `go mod download`. The tests themselves run code the agent wrote, so they get the jail's untrusted mode:
    - throwaway copies of the untouched checkout and the working copy, plus a scratch folder, are the only writable paths, so a test can't change the PR's files or poison a cache
    - no network and an empty `/run`
    - at most 512 processes, and a 10-minute timeout

    Only tests that pass on the untouched copy and fail after the edit count. They join the check findings in the one fix round, and the tests run again after it. If new failures remain (`meta.tests.left`), Sazed opens the PR as a draft. Claude Code's own Bash sandbox was tried first (plans/10 P1.11) and dropped: on CT 110 it left the agent's home writable.
  - The worker then sends the changed files (path to base64, or null for a deletion) in the finish payload.
  - The reply ends with a JSON block that Sazed turns into the PR description. If its shape is off, the worker resumes the session once to fix it; if that fails, the first reply goes through and Sazed uses its prose.
  - A change job stopped by its timeout, the tool cap or a worker shutdown still sends the files it changed so far, with `meta.stop_reason`. Sazed pauses the job and saves them as a draft PR, and a resume continues in the same working copy.
- **Repo instructions:** `--setting-sources=user` doesn't load a repo's CLAUDE.md, so a fresh review or change session gets the checkout's CLAUDE.md and REVIEW.md (capped) at the top of its prompt.
  - The gateway's GitHub App commits them on `sazed/<job-id>` and opens the PR. Nothing here can push.
- **Private repos:** they clone with a read-only, single-repo token that Sazed gets from the gateway for the job. git reads it through a credential helper that takes it from the environment, so it never appears in argv or `.git/config`. (An `http.extraheader` with Basic auth didn't work with GitHub's newer long tokens.)
- **Hard limits per job:** a wall-clock timeout and a cap on tool calls. The worker kills the agent's whole process group past either one.
- **Empty answers:** agy can end empty when its last step was a tool that headless mode refused. The worker then resumes the same conversation once and asks for an answer from what it already found.
- **Workspaces:** `~/jobs/<job-id>`, kept 14 days so follow-up jobs can resume a session.

## Sazed API it uses

All three routes are authenticated with `X-Worker-Key`:
- `POST /worker/claim` returns `204` when there is nothing to do, otherwise the job.
- `POST /worker/jobs/{id}/events` stores streamed events. It doubles as the heartbeat and answers `{"cancel": true}` if the job was cancelled.
- `POST /worker/jobs/{id}/finish` takes a status (`succeeded`, `failed` or `timed_out`), the result or error, the agent's session id, and meta (tools used, usage).

## Deploy

```bash
./deploy.sh
```

It copies `worker.py`, the `sazed_worker` package and the unit to CT 110 over SSH through the Proxmox host, then restarts the service. There's deliberately no GitHub runner on CT 110.

## Setup notes

- **agy:** headless runs need `read_url(*)` in `permissions.allow` in `~worker/.gemini/antigravity-cli/settings.json`; otherwise reading a page is refused and the answer comes back empty. Shell commands stay refused. Its Google sign-in is cached in `~worker/.gemini/antigravity-cli/antigravity-oauth-token`.
- **Claude Code:** don't run it with `--bare`, which ignores `CLAUDE_CODE_OAUTH_TOKEN`.

The full design is in plans/07 of the My-AI workspace.
