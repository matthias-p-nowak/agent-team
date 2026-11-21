# Team runner
- script `runner.py`, command line program
- shebang header `python3`
- previous work in `previous_work` - read only, reference only; new files (incl. `prompts/`) live next to `runner.py`

# Purpose
- The runner alternates between a language model acting as leader and one worker chosen by that leader. It starts with a leader call. Each call uses a separate Pi session context.
- it repeatedly runs `pi` aka `pi.dev` with arguments
    - `--provider <provider>`
    - `--model <model>`
    - `-p @prompts/<file>`
- timeout for each pi call: default 20 minutes, set with `--timeout <minutes>`
- the output (stdout+stderr) of that model is printed live
    - not for the '--list-models' call
    - assistant text (`text_delta`) in the terminal's default foreground (white on dark, black on light screens)
    - thinking deltas in gray (ANSI bright black, readable on dark and light backgrounds)
    - stderr in red
    - tool calls in blue using pi's json output: start with tool name and shortened arguments, end with error flag
- output from runner in green, and with timestamps
- no colors when stdout is not a TTY or `NO_COLOR` is set
- only the terminal's default foreground is used for the main text, so no inversion is needed for black-on-white screens

## Observed Pi JSON output
- In a test, `-p @prompts/test.md` expanded the file contents into the user message inside a `<file>` wrapper; the path was resolved relative to Pi's working directory.
- A tested `--mode json` call emits one JSON event per stdout line. Assistant text streams in `message_update.assistantMessageEvent` events with type `text_delta` and a `delta` field.
- Tool calls emit `tool_execution_start`, `tool_execution_update`, and `tool_execution_end` events, including the tool name and call ID; start includes arguments and end includes the result and `isError`.
- An assistant `message_end` contains `message.stopReason`; errors can include `message.errorMessage`. A tested nonexistent model returned `stopReason: error` even though Pi exited with status 0, so exit status alone does not establish success.
- The previous runner identifies a llama.cpp load failure by HTTP `500` and `failed to load` in the error text; this heuristic has not been verified against a live load failure in the current environment.

# Task
- the current working directory will have a folder `task`, written by the user
    - `task/vision.md`: the big-picture end result; summary of a discussion with another LLM; changes rarely
    - `task/next.md`: what the user wants to see next, preferably based on the leader's suggestion
- on 'stop', the leader writes `suggestion.md` in the current working directory
    - first line: `done` or `blocked: <reason>`
    - section "How to test" (when done)
    - section "Suggested next": the proposed next step, which the user can copy or edit into `task/next.md`
- `features.md` is a Markdown checklist (`- [ ]` open, `- [x]` done); a feature is marked done only when `suggestion.md` says `done`

# Goal
- software development, 
- one feature at a time, stop when new feature can be tested
- guide how to test the new feature
- evaluation which models work best as developers in the task
- the task needs to be first divided into a list of features
    - if `features.md` is missing, the leader creates it from `task/vision.md`; it is the leader's roadmap
    - `task/next.md` defines the feature of the current run; the leader adds it to `features.md` if absent and marks it done on 'stop'
    - if `task/next.md` is missing, empty, or already marked done, the leader only writes `suggestion.md` and stops without assigning work

# Before each leader call
- runs `pi --list-models` and fills `<cwd>/team.txt`, but skips line 1 - no output to stdout
    - each row starts with provider and model
    - discovery output is a whitespace-separated table with a header followed by model rows; the first two columns are provider and model
- remove `<cwd>/next.txt` immediately before every leader call
- randomly select a leader for the next leader call
- call leader

# State handling
- leader writes `assignment.md` for the worker
- worker writes `report.md` for status update
- record timestamps and whether `assignment.md` and `report.md` changed during each model call in `runner.log`; a change counts positively in the assessment of the model that was running
- record failures to write `assignment.md` or `report.md` against the responsible model in `runner.json`, for model evaluation outside the runner
- leader maintains `notes.md`
    - how far development has come
    - how good that called model was
    - starting point for next time the leader is called 
- check whether `notes.md` changed during each leader call and record the result with a timestamp in `runner.log`
- record a missed notes update against the leader model in `runner.json` for later evaluation; a missed update alone does not disqualify the leader or prevent processing a valid handover

# Handover
- leader write `next.txt` in current working directory
    - first line: `<prompt file>`, or 'stop' or 'disqualify'
    - second line: provider 
    - third line: model
- runner picks up `next.txt` and calls that model from that provider with that prompt
- leader should disqualify models when they do not follow assignments
- `stop` is a single-line handover; before writing it, the leader writes `suggestion.md` (see Task) and updates `features.md`
- `disqualify` uses the provider and model on lines 2 and 3 to identify the model; the leader records its reason in `notes.md`, and the runner records that the leader disqualified the model in `runner.json`
- for a worker handover, line 1 names a file inside `prompts/`, relative to the current working directory; provider and model must appear together in `team.txt`
- strip leading and trailing whitespace from each handover line; accept LF, CR, and CRLF line endings
- extra lines or missing required fields disqualify the leader

# Loop
- one leader is kept for the whole feature
- after a worker call or a 'disqualify' handover, the same leader is called again
- if the leader gets disqualified, a new random non-disqualified leader is selected and called
- 'stop' ends the feature and the run: one run = one feature; the next run starts with a new random leader

# Prompts
- at the startup, check that the current working directory has a `prompts` folder with the prompt files
- compare files in that folder with files in `prompts` next to the python runner
- if files are absent, copy them from the `prompts` folder that is next to the python script `runner.py`
- if a file exists in both but differs, keep the cwd copy and log a warning
- the runner must be called from a different directory than its own; otherwise it exits with a usage error (exit 2)
- only flat `*.md` files are copied, no subfolders
- if the runner's `prompts` folder is missing or unreadable, continue when the cwd `prompts` folder already has the required files (leader, implement, review, investigate); otherwise exit with error (exit 1)

# Logging
- to `runner.log` in current working directory
- all necessary for a coding agent to debug problems with `runner.py`
- with timestamps
- contains runner messages, Pi commands, exit codes, errors and tool events; not the streamed assistant or thinking text
- appended, no rotation

# Models that get disqualified
- cannot be loaded -> log error message from server
- got a timeout -> log "timed out"
- other Pi errors (`stopReason: error`, nonzero exit) that are not a recognised load failure -> disqualify with the error message or exit code as reason, but do not remove
    - malformed JSON lines are not fatal: print them in gray and log them
- was a leader and did not write `next.txt`
- wrote non-existing prompt/provider/model into `next.txt`
- disqualified by the leader via 'disqualify' in `next.txt`, e.g. models that do more than one step (leader's judgement)
- keep track of disqualified models in `runner.json` (see Runner state)
    - disqualified models are excluded from `team.txt` and from leader selection
    - a leader that picks a disqualified model gets disqualified itself

# Removal of models
- remove using openai/llama.cpp API - as in previous_work
- all accessible llama.cpp servers support the previous runner's removal API: for `llama-server=<url>`, send `DELETE /models?model=<model>` to that server, stripping a trailing `/v1` from its base path, and expect a JSON response with `success: true`
- log a message in log-file for each removal
- if a removal request fails, log the failure and retain the model's disqualification
- automatic, during a normal run: models that cannot be loaded are removed immediately
- optional parameter `--remove`: additionally removes all other disqualified models listed in `runner.json`

# Runner state
- `runner.json` in current working directory, maintained by `runner.py`
- `disqualified`: list of structures
    - provider
    - model
    - reason
- `settings`: current settings - per-call timeout (minutes) and run time limit (hours)
    - at startup, settings are read from the file; command line arguments override them and are written back
    - built-in defaults apply only when neither file nor command line provides a value
    - timeout and hours must be positive numbers; an invalid command line value is a usage error and the runner exits before doing anything
- persists across runs; delete the file to reset
- a corrupt or unreadable `runner.json` is renamed to `runner.json.bad`, a warning is logged, and the runner starts with defaults
- written atomically (temp file + rename)

# Run time limit
- Each invocation has a wall-clock time limit measured from runner startup. The default is 1 hour.
- The deadline applies throughout the invocation, including model discovery and model removal.
- Use `--hours <number>` to set the limit in hours. Positive fractional values are allowed.
- At the deadline, terminate any active Pi call, log that the run reached its time limit, and end the run. Leave the feature open if the leader has yet to signal `stop`.
- Apply the per-call timeout (see Purpose) to each Pi call when that timeout falls before the run deadline. Reaching the run deadline does not disqualify a model.

# Process control
- Ctrl+C: terminate the Pi process group, log "interrupted", no disqualification, exit 130
- `runner.lock` in the current working directory prevents a second runner there; removed on exit
- exit codes: 0 stop, 1 error, 2 usage error, 3 time limit reached, 4 failure stop (see below)

# Failure stop
- `--list-models` returns empty
- all models are disqualified

# Edge cases
- task files: the runner exits with error (exit 1) when `task/vision.md` is missing or unreadable; `task/next.md` handling is the leader's job (see Goal)
- matching `task/next.md` to a `features.md` entry is the leader's judgement; a `blocked` stop leaves the feature open
- `stop` validation: the runner checks that `suggestion.md` changed during that leader call; a miss is recorded against the leader in `runner.json`, the stop is still accepted
- leader prompt is `prompts/leader.md`; worker prompt names in `next.txt` are written as `prompts/<file>.md`; anything else (other folders, `..`, non-`.md`, `leader.md`) disqualifies the leader
- discovery: nonzero exit or timeout of `pi --list-models` is a failure stop (exit 4) and disqualifies nobody; malformed rows are skipped and logged; duplicate rows are merged; "empty" means no eligible models after filtering
- removal: the server URL comes from the provider `llama-server=<url>`; other providers are skipped with a log message; network errors, timeouts (10 s) and invalid responses are logged and the disqualification is kept; removal failures are never fatal
- `runner.json` evaluation data: `events` list of `{time, provider, model, role, event}` (e.g. `missed-assignment`, `missed-report`, `missed-notes`, `missed-suggestion`, `disqualified`); a model appears at most once in `disqualified`
- change detection: compare file content hashes before and after a call; a missing file counts as empty; expected files: leader -> `notes.md`, plus `assignment.md` on a worker handover, plus `suggestion.md` on `stop`; worker -> `report.md`
- lock: `runner.lock` holds the PID; a lock whose PID is no longer running is taken over with a warning; failure to create the lock exits with error (exit 1)
- state files: if `runner.json.bad` exists, use `runner.json.bad.<timestamp>`; failure to write `runner.json` or `runner.log` exits with error (exit 1)
- deadline and Ctrl+C also terminate discovery and removal; the lock is always released; when the per-call timeout and the run deadline coincide, the deadline wins (no disqualification)
- redirected output: same text without colors; tool arguments are shortened to 200 characters in output and log
- all other operational errors (e.g. prompt copy failure) exit with error (exit 1)
- failure after handover: if a leader call fails (timeout, Pi error) but left a valid `next.txt`, the failure is recorded in `runner.log` and the runner continues with the handover; the leader is not disqualified
    - same for a worker that fails but changed `report.md`: log and continue with the leader call, which judges the result
    - without a valid `next.txt` / changed `report.md`, the normal disqualification rules apply
- the leader may assign a worker call to its own provider/model (separate Pi session); this is logged
