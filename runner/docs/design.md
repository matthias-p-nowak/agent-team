# Pi runner design

The runner alternates between a language model acting as leader and one worker chosen by that leader. It starts with a leader call. Each call uses a separate Pi session context.

## Handoff

Record the objective in `runner/notes.md` before launch. The leader keeps its private assessment and decisions in that file and updates it on each call. The leader writes a self-contained assignment to `work/assignment.md`. The worker writes its outcome to `work/report.md`. The leader incorporates relevant findings into its notes and clears the report before issuing another assignment. The worker prompts use the shared `work/` files for this exchange.

`work/` is the root of the project being implemented. Start worker calls there, keep deliverables and generated files there, and give workers project paths relative to that directory. Workers can read task specifications elsewhere in the checkout for context. The leader and model discovery run from the repository root.

Runner documentation lives in `runner/docs/`. By default, the worker project's canonical `requests.md`, `design.md`, `architecture.md`, and `issues.md` live in `work/docs/`. Follow any project-local override.

Before each leader call, clear `runner/next.txt` and refresh `runner/team.txt` with `pi --list-models`. The table begins with a header and each model row starts with its provider and model. The leader chooses the worker and prompt, then writes exactly three lines to `runner/next.txt` in this order:

1. Prompt file path relative to the repository root, outside `runner/`.
2. Provider from `runner/team.txt`.
3. Model from `runner/team.txt`.

The leader can instead write `stop` on one line. Before stopping, the leader prints a user-facing outcome or, when a user decision blocks progress, one question with options.

The runner streams assistant response text during model calls. It reports thinking and tool activity on stderr and prints a status line after every 30 seconds of silence during a model call. Pi diagnostics from model calls go to stderr and appear in red when connected to a terminal and `NO_COLOR` is unset or empty. Model discovery runs quietly and reports failures through runner diagnostics.

Tool progress includes a preview of the call arguments and the completion status. Bash calls show the command, any requested timeout, and a preview of the output when the command finishes. The runner records these details alongside model progress, Pi errors, and outcomes in the local diagnostic log. Assistant response text stays on stdout.

## Model qualification and termination

Persist qualification by provider/model in `runner/memory.json`. A leader is `not tested`, `works`, or `disqualified`. A worker is `not tested`, `runs`, or `failed`. Record a reason when qualifying a model as failed or disqualified.

- Each Pi call has a 3,600-second timeout. A Pi process failure or timeout disqualifies that model from both roles. A model listing failure stops the runner because it is not attributable to an individual model.
- A successful Pi process that fails its role's file protocol loses eligibility for that role. A leader must update its notes and write a valid handoff, with a fresh assignment for worker dispatch. A worker must write a fresh report.
- When Pi reports HTTP 500 and a model-load failure from `llama-server`, request deletion of that model from its llama.cpp server cache. A failed deletion leaves the local disqualification in place.
- Keep the working leader when available and fall back to another eligible model when it fails. Stop when the leader requests it, the pool has no eligible leaders, or the configured round limit is reached.

## Prompts

[`leader.md`](../../prompts/leader.md) assesses progress and selects one worker. [`investigate.md`](../../prompts/investigate.md) collects evidence. [`implement.md`](../../prompts/implement.md) makes and verifies a change. [`review.md`](../../prompts/review.md) independently checks a result.

## Failed-load probe

The Pi command below is the server-side probe specified for observing a failed model load.

```sh
pi --provider llama-server=http://192.168.0.17:9931 --model ggml-org/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-GGUF:Q8_0 -p hello
```
