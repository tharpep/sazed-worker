"""sazed-worker: runs agent jobs (Antigravity, Claude Code) for Sazed. See plans/07.

Loop: claim a job from Sazed, run the agent CLI in a fresh workspace, stream its events back
(which doubles as the heartbeat and the cancel check), then post the result. One job at a time.

Deliberately dumb: Sazed decides the prompt, model, limits and who may start what. The worker
only enforces the hard limits it's handed (timeout, tool-call cap, which tools exist).

Isolation: this process runs as root only so it can start agents as the unprivileged `agent`
user. Agents therefore can't read this process's environment (/proc/<pid>/environ holds the
worker key and the Claude token) or /etc/sazed-eco. Each agent gets a minimal environment; Claude
Code gets only its own token. Known secret values are scrubbed from everything sent to Sazed.

Standard library only, so the container needs nothing beyond python3 and git.

Modules:
- config: environment, the agent user's paths, limits, the logger and the stop flag
- scrub, sazed_api: secret scrubbing and the Sazed worker API
- agent_process: running children as the agent user, and killing their process groups
- agy, claude: each CLI's command line and how its event stream is read
- run: one agent process, streamed to Sazed under the timeout and tool-call cap
- repo, changes, checks: checkouts, the changed-file list, and the checks run on a change
  job's edits (ruff, bash -n, gofmt/go vet, tsc)
- jobs: one job from start to finish payload
- main: the claim loop
"""
