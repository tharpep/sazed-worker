# sazed-worker

Runs agent jobs for [Sazed](https://github.com/tharpep/sazed) on an isolated homelab container (CT 110). Today that means research with Antigravity (`agy`, Gemini with Google Search grounding); Claude Code tasks come next.

Sazed owns the job queue, the prompts, the model allowlist, the daily caps and the approval gate. This service only:
- claims a job,
- runs the agent CLI in a fresh workspace,
- streams the agent's steps back (which also serves as the heartbeat and the cancel check),
- posts the result.

It runs one job at a time and uses only the Python standard library.

## How it runs

- `worker.py` runs as the unprivileged `worker` user under systemd (`sazed-worker.service`).
- **Secrets:** `SAZED_WORKER_KEY` and `CLAUDE_CODE_OAUTH_TOKEN` live in the root-only `/etc/sazed-eco/sazed-worker.env`.
  - Agent processes get a minimal environment. agy never sees either secret.
  - Claude Code will get only its own token.
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
