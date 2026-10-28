"""Settings from the environment, the agent user's paths, limits, the logger and the stop flag."""

import logging
import os
import pwd
import re
import threading
from pathlib import Path

SAZED_URL = os.environ.get("SAZED_URL", "http://192.168.50.155:8000").rstrip("/")
WORKER_KEY = os.environ.get("SAZED_WORKER_KEY", "")
CLAUDE_TOKEN = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
AGENT_USER = os.environ.get("SAZED_AGENT_USER", "agent")
_AGENT_PW = pwd.getpwnam(AGENT_USER)
AGENT_HOME = Path(_AGENT_PW.pw_dir)
AGENT_UID, AGENT_GID = _AGENT_PW.pw_uid, _AGENT_PW.pw_gid
JOBS_DIR = AGENT_HOME / "jobs"
AGY = str(AGENT_HOME / ".local/bin/agy")
CLAUDE = str(AGENT_HOME / ".local/bin/claude")
RUFF = "/usr/local/bin/ruff"  # lint pre-check for change jobs (run by the worker, not the agent)
POLL_SECONDS = 5
FLUSH_SECONDS = 2
MAX_PENDING_EVENTS = 2000
WORKSPACE_KEEP_DAYS = 14
DEADLINE_GRACE_S = 30  # extra seconds past the job timeout before the watchdog kills the agent
TOOL_RESULT_PREVIEW = 500  # characters of each Claude tool result kept in the saved events
SAFE_ARG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")

log = logging.getLogger("sazed-worker")
stop = threading.Event()  # set by SIGTERM/SIGINT: the running agent is killed, the loop ends
