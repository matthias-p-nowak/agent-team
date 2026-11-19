#!/usr/bin/env python3
"""Run alternating Pi leader and worker calls from the repository root."""

import argparse
import codecs
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
from tempfile import NamedTemporaryFile
from textwrap import indent
import time
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


# Anchor all control files and subprocesses to this checkout.
ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "runner"
LOG_PATH = RUNNER / "runner.log"
WORK = ROOT / "work"
MEMORY = RUNNER / "memory.json"
TEAM = RUNNER / "team.txt"
NOTES = RUNNER / "notes.md"
NEXT = RUNNER / "next.txt"
ASSIGNMENT = WORK / "assignment.md"
REPORT = WORK / "report.md"
LEADER_PROMPT = "prompts/leader.md"
PI_TIMEOUT = 3600
PI_PROGRESS_INTERVAL = 30  # Seconds between status lines when a model call is quiet.
TOOL_ARGS_LIMIT = 2000  # Characters shown for tool arguments or the Bash command.
TOOL_OUTPUT_LIMIT = 4096  # Characters shown from the end of a tool result.
DEFAULT_MAX_ROUNDS = 20
Model = tuple[str, str]  # Provider and exact model ID as listed by Pi.
LOGGER = logging.getLogger("pi_runner")
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False
LOGGER.addHandler(logging.NullHandler())


def log(message):
    """Write runner diagnostics to stderr and the local log."""
    LOGGER.info("%s", message)
    print(message, file=sys.stderr, flush=True)


def write_atomic(path, content):
    """Replace a state file once its new content has been written."""
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)
        try:
            temporary.write(content)
            temporary.close()
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)


def save_memory(memory):
    """Persist model qualifications so a new run can reuse them."""
    write_atomic(MEMORY, json.dumps(memory, indent=2, sort_keys=True) + "\n")


def model_key(model: Model) -> str:
    """Return the provider/model key used in memory.json."""
    return "/".join(model)


def tool_preview(text, limit, *, tail=False):
    """Bound tool details and display control characters literally."""
    if len(text) > limit:
        text = f"[Earlier output omitted]\n{text[-limit:]}" if tail else f"{text[:limit]} [truncated]"
    return "".join(char if char.isprintable() or char in "\n\t" else ascii(char)[1:-1] for char in text)


class ModelOutput:
    """Render Pi's event stream as response text and progress, and track its final error."""

    def __init__(self):
        self.pending = []
        self.text_started = False
        self.thinking = False
        self.stop_reason = None
        self.error = None
        self.last_output = time.monotonic()

    def progress(self, message):
        """Show runner progress and restart the quiet period."""
        log(message)
        self.last_output = time.monotonic()

    def tool_started(self, event):
        """Report a tool's resolved arguments when it begins executing."""
        name = event["toolName"]
        args = event.get("args", {})
        if name == "bash" and isinstance(args, dict) and isinstance(args.get("command"), str):
            timeout = args.get("timeout")
            timeout_label = f" (timeout {timeout}s)" if isinstance(timeout, (int, float)) else ""
            command = indent("$ " + tool_preview(args["command"], TOOL_ARGS_LIMIT), "  ")
            self.progress(f"Pi is running bash{timeout_label}\n{command}")
        else:
            preview = tool_preview(json.dumps(args, ensure_ascii=False), TOOL_ARGS_LIMIT)
            self.progress(f"Pi is running tool {name} {preview}")

    def tool_finished(self, event):
        """Report tool status and show the result of Bash calls and failed tools."""
        name = event["toolName"]
        result = event.get("result") or {}
        status = "failed" if event.get("isError") else "finished"
        exit_code = (result.get("structuredContent") or {}).get("exit_code") if name == "bash" else None
        exit_label = f" (exit {exit_code})" if isinstance(exit_code, int) else ""
        duration = event.get("durationMs")
        duration_label = f" in {duration / 1000:.2f}s" if isinstance(duration, (int, float)) else ""
        message = f"Pi tool {name} {status}{exit_label}{duration_label}"
        if name == "bash" or event.get("isError"):
            output = "\n".join(block["text"] for block in result.get("content", []) if block["type"] == "text")
            if output:
                message += "\n" + indent(tool_preview(output.rstrip("\n"), TOOL_OUTPUT_LIMIT, tail=True), "  ")
        self.progress(message)

    def feed(self, text, *, final):
        """Yield display text from complete events, including an unterminated final line."""
        *lines, tail = text.split("\n")
        if final:
            lines.append(tail)
        for line in lines:
            self.pending.append(line)
            record = "".join(self.pending)
            self.pending.clear()
            if record:
                yield self.render_event(record)
        if not final and tail:
            self.pending.append(tail)

    def render_event(self, record):
        """Extract response text and preserve the failure semantics of Pi's text mode."""
        try:
            event = json.loads(record)
        except json.JSONDecodeError:
            return record + "\n"
        if not isinstance(event, dict):
            return record + "\n"
        event_type = event.get("type")
        if event_type == "message_update":
            update = event["assistantMessageEvent"]
            update_type = update["type"]
            if update_type == "text_delta" and update["delta"]:
                self.text_started = True
                return update["delta"]
            if update_type in ("thinking_start", "thinking_delta") and not self.thinking:
                self.thinking = True
                self.progress("Pi is thinking...")
            elif update_type == "thinking_end":
                self.thinking = False
            elif update_type == "toolcall_start":
                name = update.get("toolName")
                self.progress(f"Pi is preparing tool {name}..." if name else "Pi is preparing a tool call...")
        elif event_type == "tool_execution_start":
            self.tool_started(event)
        elif event_type == "tool_execution_end":
            self.tool_finished(event)
        elif event_type == "message_end" and event["message"].get("role") == "assistant":
            message = event["message"]
            self.thinking = False
            stop_reason = message.get("stopReason")
            self.stop_reason = stop_reason
            text = "\n" if self.text_started else "".join(
                block["text"] + "\n" for block in message.get("content", [])
                if block["type"] == "text" and block["text"]
            )
            self.text_started = False
            self.error = None
            if stop_reason in ("error", "aborted"):
                self.error = message.get("errorMessage") or f"Request {stop_reason}"
            elif stop_reason == "length":
                self.error = "Response was truncated: the model reached its output token limit."
                if not text:
                    self.error += " Pi returned no answer text in that response."
            return text
        return ""


def relay_output(destination, text, *, red):
    """Flush visible output and reset terminal color after each stderr write."""
    destination.write(f"\x1b[31m{text}\x1b[0m" if red else text)
    destination.flush()


def stream_pi_output(process, command, model_output, *, display):
    """Capture both Pi pipes before the deadline and optionally relay them as data arrives."""
    started = time.monotonic()
    deadline = started + PI_TIMEOUT
    stdout, stderr = [], []
    red_stderr = sys.stderr.isatty() and not os.environ.get("NO_COLOR")
    with selectors.DefaultSelector() as selector:
        for pipe, destination, chunks, red, events in (
            (process.stdout, sys.stdout, stdout, False, model_output),
            (process.stderr, sys.stderr, stderr, red_stderr, None),
        ):
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            selector.register(pipe, selectors.EVENT_READ, (destination, chunks, red, decoder, events))

        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, PI_TIMEOUT)
            if model_output:
                until_status = model_output.last_output + PI_PROGRESS_INTERVAL - time.monotonic()
                if until_status <= 0:
                    model_output.progress("Pi is still running...")
                    until_status = PI_PROGRESS_INTERVAL
                remaining = min(remaining, until_status)
            for key, _ in selector.select(remaining):
                destination, chunks, red, decoder, events = key.data
                data = os.read(key.fd, 4096)
                text = decoder.decode(data, final=not data)
                rendered = events.feed(text, final=not data) if events else (text,)
                for visible in rendered:
                    if visible:
                        chunks.append(visible)
                        if destination is sys.stderr:
                            LOGGER.warning("Pi stderr: %s", visible.rstrip("\n"))
                        if display:
                            relay_output(destination, visible, red=red)
                        if model_output:
                            model_output.last_output = time.monotonic()
                if not data:
                    selector.unregister(key.fileobj)

    process.wait(timeout=max(0, deadline - time.monotonic()))
    returncode = process.returncode
    if model_output and model_output.error:
        error = model_output.error + "\n"
        stderr.append(error)
        LOGGER.warning("Pi model error: %s", model_output.error)
        relay_output(sys.stderr, error, red=red_stderr)
        returncode = returncode or 1
    output = "".join(stdout)
    if model_output:
        LOGGER.info(
            "Pi finished in %.1fs (process_exit=%d, status=%d, stop_reason=%s, stdout_chars=%d)",
            time.monotonic() - started, process.returncode, returncode, model_output.stop_reason, len(output),
        )
    return subprocess.CompletedProcess(command, returncode, output, "".join(stderr))


def run_pi(*arguments, cwd=ROOT, model_events=False, display=True):
    """Run Pi with a deadline and clean up its process group on interruption."""
    command = ["pi", *arguments]
    with subprocess.Popen(
        command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, start_new_session=True,
    ) as process:
        try:
            return stream_pi_output(process, command, ModelOutput() if model_events else None, display=display)
        except (subprocess.TimeoutExpired, KeyboardInterrupt, OSError):
            # Let Pi clean up its own tool processes, then kill any remaining group members.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            raise


def refresh_team(memory):
    """Refresh the team's model table and register any newly discovered models."""
    try:
        result = run_pi("--list-models", display=False)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"pi --list-models timed out after {PI_TIMEOUT} seconds") from error
    if result.returncode:
        raise RuntimeError(f"pi --list-models exited with {result.returncode}")

    rows = iter(result.stdout.splitlines(keepends=True))
    for header in rows:
        if header.split()[:2] == ["provider", "model"]:
            break
    else:
        raise RuntimeError("pi --list-models returned no model table")

    models = {}
    for row in rows:
        fields = row.split()
        if len(fields) < 2:
            continue
        model = (fields[0], fields[1])
        models.setdefault(model, row)
        memory.setdefault(model_key(model), {
            "leader": "not tested", "worker": "not tested", "disqualification_reason": None,
        })

    write_atomic(TEAM, header + "".join(models.values()))
    save_memory(memory)
    if not models:
        raise RuntimeError("pi --list-models returned no available models")
    return list(models)


def choose_leader(models, memory, preferred):
    """Keep an eligible preferred leader or select an eligible replacement."""
    eligible = [model for model in models if memory[model_key(model)]["leader"] != "disqualified"]
    if preferred in eligible:
        return preferred
    return next(
        (model for model in eligible if memory[model_key(model)]["leader"] == "works"),
        eligible[0] if eligible else None,
    )


def delete_failed_model(model, error_output):
    """Request cache deletion for the failed-load error specified by the design."""
    provider, name = model
    if not provider.startswith("llama-server="):
        return
    if not (re.search(r"\b500\b", error_output) and re.search(r"\bfailed\s+to\s+load\b", error_output, re.I)):
        return

    try:
        server = urlsplit(provider.split("=", 1)[1])
        if server.scheme not in {"http", "https"} or not server.hostname:
            raise ValueError("invalid llama-server URL")
        base_path = server.path.rstrip("/").removesuffix("/v1")
        url = urlunsplit((server.scheme, server.netloc, f"{base_path}/models", urlencode({"model": name}), ""))
        with urlopen(Request(url, method="DELETE"), timeout=10) as response:
            payload = json.load(response)
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise ValueError("server did not confirm deletion")
    except (OSError, ValueError) as error:
        log(f"Could not delete {model_key(model)} from llama-server: {error}")
    else:
        log(f"Deleted {model_key(model)} from the llama-server cache.")


def run_model(model, prompt, memory, *, cwd=ROOT):
    """Run a repository prompt from the role's working directory."""
    provider, name = model
    prompt_path = os.path.relpath(ROOT / prompt, cwd)
    error_output = ""
    try:
        result = run_pi(
            "--no-session", "--provider", provider, "--model", name, "--mode", "json", "-p", f"@{prompt_path}",
            cwd=cwd, model_events=True,
        )
    except subprocess.TimeoutExpired:
        reason = f"Pi timed out after {PI_TIMEOUT} seconds"
    else:
        if result.returncode == 0:
            return True
        error_output = result.stderr.strip() or result.stdout.strip()
        reason = f"Pi exited with {result.returncode}"

    detail = f"{reason}: {error_output}" if error_output else reason
    memory[model_key(model)].update(leader="disqualified", worker="failed", disqualification_reason=detail)
    save_memory(memory)
    log(f"Disqualified {model_key(model)}: {reason}")
    if error_output:
        delete_failed_model(model, error_output)
    return False


def file_snapshot(path):
    """Capture file identity, modification time, and content for freshness checks."""
    try:
        with path.open("rb") as file:
            stat = os.fstat(file.fileno())
            return stat.st_ino, stat.st_mtime_ns, file.read()
    except FileNotFoundError:
        return None


def read_handoff(models, memory):
    """Return the validated prompt and worker, or None for a stop request."""
    if not NEXT.is_file():
        raise ValueError("leader did not write runner/next.txt")
    lines = NEXT.read_text(encoding="utf-8").splitlines()
    if lines == ["stop"]:
        return None
    if len(lines) != 3 or any(not line or line != line.strip() for line in lines):
        raise ValueError("runner/next.txt must contain stop or three nonempty lines")

    prompt, provider, name = lines
    try:
        path = (ROOT / prompt).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"invalid prompt path in runner/next.txt: {prompt}") from error
    if (Path(prompt).is_absolute() or not path.is_relative_to(ROOT)
            or path.is_relative_to(RUNNER) or not path.is_file()):
        raise ValueError(f"invalid prompt path in runner/next.txt: {prompt}")
    worker = (provider, name)
    if worker not in models:
        raise ValueError(f"worker is absent from runner/team.txt: {model_key(worker)}")
    if memory[model_key(worker)]["worker"] == "failed":
        raise ValueError(f"worker is marked failed: {model_key(worker)}")
    return prompt, worker


def run_rounds(max_rounds, preferred):
    """Alternate model calls until the leader stops or an execution limit is hit."""
    memory = json.loads(MEMORY.read_text(encoding="utf-8")) if MEMORY.exists() else {}
    if not isinstance(memory, dict) or any(
        not isinstance(record, dict)
        or record.get("leader") not in ("not tested", "works", "disqualified")
        or record.get("worker") not in ("not tested", "runs", "failed")
        for record in memory.values()
    ):
        raise ValueError("runner/memory.json contains invalid model qualifications")
    WORK.mkdir(exist_ok=True)
    for round_number in range(1, max_rounds + 1):
        NEXT.unlink(missing_ok=True)
        models = refresh_team(memory)
        if round_number == 1 and preferred is not None and preferred not in models:
            raise RuntimeError(f"requested leader is not in runner/team.txt: {model_key(preferred)}")
        leader = choose_leader(models, memory, preferred)
        if leader is None:
            raise RuntimeError("no eligible leader remains")

        before_notes = file_snapshot(NOTES)
        before_assignment = file_snapshot(ASSIGNMENT)
        log(f"Round {round_number}/{max_rounds}, leader {model_key(leader)}")
        if not run_model(leader, LEADER_PROMPT, memory):
            continue

        leader_record = memory[model_key(leader)]
        try:
            after_notes = file_snapshot(NOTES)
            if after_notes is None or not after_notes[2].strip() or after_notes == before_notes:
                raise ValueError("leader did not update runner/notes.md")
            handoff = read_handoff(models, memory)
            if handoff is not None:
                assignment = file_snapshot(ASSIGNMENT)
                if assignment is None or not assignment[2].strip() or assignment == before_assignment:
                    raise ValueError("leader did not write a fresh work/assignment.md")
        except ValueError as error:
            leader_record.update(leader="disqualified", disqualification_reason=str(error))
            save_memory(memory)
            log(f"Disqualified {model_key(leader)} as leader: {error}")
            continue

        leader_record["leader"] = "works"
        save_memory(memory)
        if handoff is None:
            return 0

        preferred = leader
        prompt, worker = handoff
        REPORT.unlink(missing_ok=True)
        log(f"Round {round_number}/{max_rounds}, worker {model_key(worker)} using {prompt}")
        if not run_model(worker, prompt, memory, cwd=WORK):
            continue
        worker_record = memory[model_key(worker)]
        report = file_snapshot(REPORT)
        if report is None or not report[2].strip():
            reason = "worker did not write work/report.md"
            worker_record.update(worker="failed", disqualification_reason=reason)
            log(f"Disqualified {model_key(worker)} as worker: {reason}")
        else:
            worker_record["worker"] = "runs"
        save_memory(memory)

    log(f"Reached the limit of {max_rounds} leader rounds.")
    return 1


def main():
    """Parse CLI options and run with exclusive access to the control files."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--leader", nargs=2, metavar=("PROVIDER", "MODEL"), help="initial leader to try")
    parser.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS,
                        help=f"maximum leader calls (default: {DEFAULT_MAX_ROUNDS})")
    args = parser.parse_args()
    if args.max_rounds < 1:
        parser.error("--max-rounds must be at least 1")
    preferred = (args.leader[0], args.leader[1]) if args.leader else None

    lock = None
    log_handler = None
    try:
        # Lock the runner directory without adding a persistent lock file.
        lock = os.open(RUNNER, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another runner is active in this workspace") from None

        log_handler = RotatingFileHandler(LOG_PATH, maxBytes=1024 * 1024, backupCount=2, encoding="utf-8")
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ")
        formatter.converter = time.gmtime
        log_handler.setFormatter(formatter)
        LOGGER.addHandler(log_handler)
        LOGGER.info("Runner started (pid=%d, max_rounds=%d, preferred=%s)", os.getpid(), args.max_rounds, preferred)
        status = run_rounds(args.max_rounds, preferred)
        LOGGER.info("Runner exited with status %d", status)
        return status
    except KeyboardInterrupt:
        log("Runner interrupted.")
        return 130
    except (OSError, RuntimeError, ValueError) as error:
        log(f"Runner error: {error}")
        return 1
    except Exception:
        LOGGER.exception("Unexpected runner error")
        raise
    finally:
        if log_handler is not None:
            LOGGER.removeHandler(log_handler)
            log_handler.close()
        if lock is not None:
            os.close(lock)


if __name__ == "__main__":
    sys.exit(main())
