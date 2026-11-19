# Pi runner architecture

## Running the loop

Put the objective in `runner/notes.md`, then run `python3 runner/runner.py`. The runner uses the `pi` on `PATH` and uses `--no-session` for model calls. Use `--leader PROVIDER MODEL` to choose the initial leader or `--max-rounds N` to change the default limit of 20 leader calls. A leader stop exits with status `0`. Runner errors and the round limit exit with `1`. Ctrl-C exits with `130`.

## State and subprocesses

The Python runner locks its state directory during a run. It replaces `team.txt` and `memory.json` atomically and checks that a leader updates its notes and assignment before accepting a worker handoff. Worker prompt files must resolve inside the checkout and outside `runner/`. Pi subprocesses run in their own process groups so the runner can terminate them on timeout or interruption.

The runner reads both Pi pipes concurrently and flushes visible output as it arrives. Model calls use `--mode json` to render assistant text deltas and detect model errors reported in the final message event. The runner retains decoded output for model discovery and failure handling.

Model discovery and leader calls start at the repository root. Worker calls start inside `work/`, with the selected prompt path resolved relative to that directory. The leader owns `runner/notes.md`. Both roles execute with the workspace's filesystem permissions, so note privacy relies on the role instructions and the runner's prompt-path validation.

For the failed-load recovery defined in the [design](design.md), the runner sends `DELETE /models?model=…` to the llama.cpp router configured by the model's provider. The router must support cache deletion for that request to succeed. [llama.cpp server API](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md#delete-models-delete-a-model-from-cache)
