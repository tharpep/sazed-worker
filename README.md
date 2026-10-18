# sazed-worker

Runs agent jobs for [Sazed](https://github.com/tharpep/sazed) on an isolated homelab container (CT 110). Research and second opinions run on Antigravity (`agy`, Gemini with Google Search grounding); read-only code reviews of public repos run on Claude Code. PR-only code changes come next.

Sazed owns the job queue, the prompts, the model allowlist, the daily caps and the approval gate. This service only:
- claims a job,
- runs the agent CLI in a fresh workspace,
- streams the agent's steps back (which also serves as the heartbeat and the cancel check),
- posts the result.

It runs one job at a time and uses only the Python standard library.

## How it runs

- **Users:** `worker.py` runs as root under systemd (`sazed-worker.service`), but only so it can start every agent, and every git command, as the unprivileged `agent` user. The CLIs and their logins live in `/home/agent`.
- **Why a separate user:** an agent can't read the worker's `/proc/<pid>/environ` (worker key, Claude token) or the root-only `/etc/sazed-eco/sazed-worker.env`. A same-user agent could, as a Claude Code review of this repo pointed out.
- **Agent environment:** minimal. Claude Code gets only its own token.
- **Scrubbing:** known secret values are removed from everything posted to Sazed.
- **Review profile:** `--tools=Read,Grep,Glob` only (no Bash: even git diff can write files with `--output=`).
  - It refuses reads of the agent's own logins and `/proc`.
  - `--setting-sources=user` ignores a repo's `.claude/settings.json` hooks.
- **Unit gotcha:** no explicit `User=root` in the unit. With `NoNewPrivileges=yes`, systemd then drops CAP_SETUID and the privilege drop fails.
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

It copies `worker.py` and the unit to CT 110 over SSH through the Proxmox host, then restarts the service. There's deliberately no GitHub runner on CT 110.

## Setup notes

- **agy:** headless runs need `read_url(*)` in `permissions.allow` in `~worker/.gemini/antigravity-cli/settings.json`; otherwise reading a page is refused and the answer comes back empty. Shell commands stay refused. Its Google sign-in is cached in `~worker/.gemini/antigravity-cli/antigravity-oauth-token`.
- **Claude Code:** don't run it with `--bare`, which ignores `CLAUDE_CODE_OAUTH_TOKEN`.

The full design is in plans/07 of the My-AI workspace.
