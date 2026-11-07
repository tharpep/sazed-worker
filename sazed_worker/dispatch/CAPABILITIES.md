# CAPABILITIES

Written by `sazed_worker/dispatch/probe.py` on 2026-11-07, Claude Code 2.1.319 (Claude Code), Linux, no managed policy.
Re-run after a Claude Code upgrade. Read the evidence column, not the result.

| Claim | Holds | Evidence |
| --- | --- | --- |
| auth: headless auth with --setting-sources project | yes | exit 0, result 'ok', 7.8 s |
| cost: total_cost_usd present in the result event | yes | total_cost_usd = 0.00152554 |
| models: haiku, sonnet, opus aliases resolve | yes | haiku → exit 0, claude-haiku-5-5; sonnet → exit 0, claude-sonnet-5-5; opus → exit 0, claude-opus-5-5 |
| schema: --json-schema returns structured_output | yes | structured_output = {'state': 'done'} |
| resume: claude -p --resume recalls the first answer | yes | session 22fe0d04-8ad3-40d9-918f-6785e46ce1e8, second answer 'heliotrope' |
| hooks + push: a --settings PreToolUse hook fires and refuses git push | yes | hook log has the push; remote main = absent |
| commit: with Bash allowed in --settings, an agent commits headless | yes | HEAD subject 'probe commit'; tools ['Bash']; last output ['[main 4adceea] probe commit\n 1 file changed, 1 insertion(+)\n create mode 100644 note.txt'] |
| deny-rule-push: Bash allowed, a Bash(git push:*) deny rule still refuses the push | yes | Bash allowed, Bash(git push:*) denied: remote main = absent; outputs ['Permission to use Bash with command git push origin main has been denied.'] |
| acceptEdits-bash: acceptEdits admits a file-edit command (touch), default mode denies it | yes | acceptEdits: touch ran; default: touch denied |
| settings-deny: permissions.deny in --settings is enforced headless | yes | deny Write, Edit, Bash in --settings: file refused; tools ['Write']; outputs ['<tool_use_error>Error: No such tool available: Write. Write is disabled for this session, in subagents as well as here.</tool_use_error>'] |
| read-only: --disallowedTools removes Edit/Write/Bash | yes | init tools lack Edit/Write/Bash: True; write landed: False |
| budget: --max-budget-usd stops a run over its cap | yes | exit 1, subtype 'error_max_budget_usd' |
| effort: --effort max thinks more than low | yes | output tokens low 232, max 674 |
| bash-timeout: the timeout variables keep a 185 s command in the foreground | yes | 1 Bash call(s), backgrounded False, 191.7 s, outputs ['finished'] |
