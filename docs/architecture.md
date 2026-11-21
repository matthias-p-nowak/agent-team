# Runner architecture

`runner.py` runs from a task working directory separate from its own installation. It copies missing default Markdown prompts from `prompts/`, discovers eligible Pi models before each leader call, and alternates the retained leader with selected workers until a stop or failure. Prompt instructions delegate feature tracking and development decisions to the leader; the runner validates the handover and records file changes and missed updates.

The runner stores settings, disqualifications and evaluation events atomically in `runner.json`, appends diagnostics to `runner.log`, and uses a PID lock to prevent concurrent runs in one directory. Pi subprocesses run in separate process groups with streaming JSON output, per-call timeouts and an invocation deadline. Failed llama.cpp loads trigger model removal; `--remove` also attempts removal of existing disqualifications. See [design](design.md) for the behavior and edge cases.
