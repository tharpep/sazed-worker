"""Agent adapters: one module per harness, all behind one contract (My-AI plans/14, contract 2).

A third harness is one more module here. Each provides:
- NAME, CAPABILITIES (the profiles it can run), RESUMES, TOOL_POLICY, READS_CONTEXT (rule files
  it loads from the checkout on its own, so the worker doesn't paste them into the prompt)
- available(): its CLI, and any login it needs, is on this machine
- extra_env(): what its process needs beyond the base agent environment
- command(job, prompt, resume): the argv for one run
- handle_event(run, event): session id, tool count, plan limits, the normalized result
- trim_event(event): the event as saved, or None to drop it
- after_exit(run, code), optional: fallbacks once the process has exited
"""

from types import ModuleType

from . import agy, claude

# Order is kept for the startup log and the claim's agent list.
ADAPTERS: dict[str, ModuleType] = {a.NAME: a for a in (agy, claude)}


def get(name: str) -> ModuleType | None:
    return ADAPTERS.get(name)


def available() -> list[str]:
    return [name for name, adapter in ADAPTERS.items() if adapter.available()]
