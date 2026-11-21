#!/usr/bin/env python3
"""Run one development feature through alternating Pi leader and worker calls."""

import argparse
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import random
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
CWD = Path.cwd()
REQUIRED = {"leader.md", "implement.md", "review.md", "investigate.md"}
COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ
START = time.monotonic()


class Deadline(Exception):
    """The invocation's overall deadline was reached."""


class Failure(Exception):
    """An operational or discovery failure with a specified exit status."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def stamp():
    """Return a timestamp suitable for logs and state events."""
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def display(message, color=None, end="\n"):
    """Display a message, with optional terminal color."""
    prefix = f"\033[{color}m" if COLOR and color else ""
    suffix = "\033[0m" if prefix else ""
    print(prefix + str(message) + suffix, end=end, flush=True)


def log(message, show=False):
    """Append a timestamped diagnostic and optionally show it to the user."""
    line = f"{stamp()} {message}"
    with (CWD / "runner.log").open("a", encoding="utf-8") as out:
        out.write(line + "\n")
    if show:
        display(line, "32")


def positive(value):
    """Parse a positive finite command-line number."""
    try:
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise ValueError
        return number
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive finite number")


def digest(name):
    """Hash file contents, treating an absent file as empty."""
    try:
        data = (CWD / name).read_bytes()
    except FileNotFoundError:
        data = b""
    return hashlib.sha256(data).digest()


def save(state):
    """Atomically persist runner state in the working directory."""
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=CWD, prefix=".runner-", delete=False) as out:
            name = out.name
            json.dump(state, out, indent=2)
            out.write("\n")
        os.replace(name, CWD / "runner.json")
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def load():
    """Load state, preserving unreadable or invalid state for inspection."""
    path = CWD / "runner.json"
    if not path.exists():
        return {"disqualified": [], "settings": {}, "events": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not isinstance(state.get("disqualified", []), list) or not isinstance(state.get("settings", {}), dict) or not isinstance(state.get("events", []), list):
            raise ValueError("invalid state structure")
        for key, default in (("disqualified", []), ("settings", {}), ("events", [])):
            state.setdefault(key, default)
        for item in state["disqualified"]:
            if not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in ("provider", "model", "reason")):
                raise ValueError("invalid disqualification")
        for key in ("timeout", "hours"):
            if key in state["settings"]:
                value = state["settings"][key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                    raise ValueError("invalid setting")
        return state
    except (OSError, ValueError, TypeError) as exc:
        bad = CWD / "runner.json.bad"
        if bad.exists():
            bad = CWD / ("runner.json.bad." + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S%f"))
        path.rename(bad)
        log(f"Warning: invalid runner.json moved to {bad.name}: {exc}", True)
        return {"disqualified": [], "settings": {}, "events": []}


def event(state, pair, role, kind):
    """Record an evaluation event and persist it."""
    state["events"].append(dict(time=stamp(), provider=pair[0], model=pair[1], role=role, event=kind))
    save(state)


def disqualify(state, pair, role, reason):
    """Exclude a model from subsequent calls and record the reason."""
    if not any((x["provider"], x["model"]) == pair for x in state["disqualified"]):
        state["disqualified"].append(dict(provider=pair[0], model=pair[1], reason=reason))
        event(state, pair, role, "disqualified")
    log(f"Disqualified {pair[0]} {pair[1]}: {reason}", True)


def prepare_prompts():
    """Copy missing default prompts and preserve local customizations."""
    target = CWD / "prompts"
    target.mkdir(exist_ok=True)
    source = ROOT / "prompts"
    try:
        files = list(source.glob("*.md")) if source.is_dir() else []
        for src in files:
            dst = target / src.name
            if not dst.exists():
                shutil.copyfile(src, dst)
            elif src.read_bytes() != dst.read_bytes():
                log(f"Warning: preserving different local prompt {dst}", True)
    except OSError as exc:
        if not all((target / name).is_file() and os.access(target / name, os.R_OK) for name in REQUIRED):
            raise Failure(f"Cannot prepare prompts: {exc}") from exc
        log(f"Warning: cannot read default prompts: {exc}", True)
    if not all((target / name).is_file() and os.access(target / name, os.R_OK) for name in REQUIRED):
        raise Failure("Required prompts are missing or unreadable")


def acquire_lock():
    """Acquire the working-directory PID lock, recovering a stale lock."""
    path = CWD / "runner.lock"
    for attempt in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as out:
                out.write(str(os.getpid()))
            return
        except FileExistsError:
            try:
                pid = int(path.read_text().strip())
                if pid <= 0:
                    raise ValueError
                os.kill(pid, 0)
            except ProcessLookupError:
                log(f"Warning: removing stale lock for PID {pid}", True)
                path.unlink()
                continue
            except (ValueError, OSError) as exc:
                raise Failure(f"Cannot verify existing runner lock: {exc}") from exc
            raise Failure(f"Another runner holds the lock (PID {pid})")
    raise Failure("Could not acquire runner lock")


def terminate(proc):
    """Terminate a subprocess group and reap it."""
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        except ProcessLookupError:
            proc.wait()


def run_pi(args, deadline, timeout, stream):
    """Run Pi with a deadline, streaming JSON events and capturing errors."""
    cmd = ["pi"] + args
    log("Command: " + shlex.join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    selector = selectors.DefaultSelector()
    for pipe, kind in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
        os.set_blocking(pipe.fileno(), False)
        selector.register(pipe, selectors.EVENT_READ, kind)
    buffers = {"stdout": b"", "stderr": b""}
    output, errors, pi_errors = [], [], []
    call_end = time.monotonic() + timeout

    def line(kind, raw):
        text = raw.decode("utf-8", errors="replace")
        if not stream:
            (output if kind == "stdout" else errors).append(text)
            return
        if kind == "stderr":
            errors.append(text)
            display(text, "31")
            log("Pi stderr: " + text)
            return
        try:
            obj = json.loads(text)
            if not isinstance(obj, dict):
                raise ValueError("expected an object")
        except (ValueError, TypeError):
            display(text, "90")
            log("Malformed Pi JSON: " + text)
            return
        kind_event = obj.get("type")
        if kind_event == "message_update":
            update = obj.get("assistantMessageEvent") or {}
            if update.get("type") in ("text_delta", "thinking_delta"):
                display(update.get("delta", ""), "90" if update.get("type") == "thinking_delta" else None, end="")
        elif kind_event in ("tool_execution_start", "tool_execution_update", "tool_execution_end"):
            tool = obj.get("toolName", "tool")
            if kind_event == "tool_execution_start":
                detail = json.dumps(obj.get("args", {}), ensure_ascii=False)[:200]
                msg = f"Tool start: {tool} {detail}"
            elif kind_event == "tool_execution_end":
                msg = f"Tool end: {tool} error={obj.get('isError', False)}"
            else:
                msg = f"Tool update: {tool}"
            display("\n" + msg, "34")
            log(msg)
        elif kind_event == "message_end":
            message = obj.get("message") or {}
            if message.get("stopReason") == "error":
                pi_errors.append(str(message.get("errorMessage") or "Pi reported an error"))

    timed_out = False
    try:
        while selector.get_map():
            now = time.monotonic()
            if now >= deadline:
                raise Deadline()
            if now >= call_end:
                timed_out = True
                break
            for key, _ in selector.select(min(deadline - now, call_end - now, 0.2)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                kind = key.data
                if not chunk:
                    selector.unregister(key.fileobj)
                    if buffers[kind]:
                        line(kind, buffers[kind])
                        buffers[kind] = b""
                    continue
                buffers[kind] += chunk
                while b"\n" in buffers[kind]:
                    raw, buffers[kind] = buffers[kind].split(b"\n", 1)
                    line(kind, raw.rstrip(b"\r"))
        if timed_out:
            terminate(proc)
        else:
            while proc.poll() is None:
                if time.monotonic() >= deadline:
                    raise Deadline()
                if time.monotonic() >= call_end:
                    timed_out = True
                    terminate(proc)
                    break
                time.sleep(0.05)
    finally:
        terminate(proc)
        selector.close()
        proc.stdout.close()
        proc.stderr.close()
    log(f"Pi exit code: {proc.returncode}" + (" (timed out)" if timed_out else ""))
    return output, errors, pi_errors, proc.returncode, timed_out


def discover(state, deadline, timeout):
    """Refresh team.txt from Pi's model table, excluding disqualified models."""
    out, err, _, code, timed = run_pi(["--list-models"], deadline, timeout, False)
    if code or timed:
        raise Failure("Model discovery failed" + (": timed out" if timed else f" (exit {code}): {' '.join(err)}"), 4)
    excluded = {(x["provider"], x["model"]) for x in state["disqualified"]}
    rows = {}
    for line in out[1:]:
        fields = line.split()
        if len(fields) < 2:
            if line.strip():
                log(f"Malformed discovery row: {line}")
            continue
        pair = tuple(fields[:2])
        if pair not in excluded:
            rows.setdefault(pair, line)
    (CWD / "team.txt").write_text("".join(row + "\n" for row in rows.values()), encoding="utf-8")
    if not rows:
        raise Failure("No eligible models available", 4)
    return set(rows)


def handover(team):
    """Parse and validate the leader's handover against the current team."""
    try:
        lines = [line.strip() for line in (CWD / "next.txt").read_text(encoding="utf-8").splitlines()]
    except (OSError, UnicodeError):
        return None
    if lines == ["stop"]:
        return ("stop", None)
    if len(lines) != 3 or not all(lines):
        return None
    action, provider, model = lines
    pair = (provider, model)
    if pair not in team:
        return None
    if action == "disqualify":
        return (action, pair)
    path = Path(action)
    if len(path.parts) != 2 or path.parts[0] != "prompts" or path.name in ("leader.md", ".", "..") or path.suffix != ".md" or not (CWD / path).is_file():
        return None
    return (action, pair)


def remove_model(pair, deadline):
    """Request model removal from a llama.cpp provider if applicable."""
    provider, model = pair
    if not provider.startswith("llama-server="):
        log(f"Skipping removal for non-llama provider {provider} {model}")
        return
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Deadline()
    base = provider[len("llama-server="):].rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    url = base + "/models?" + urllib.parse.urlencode({"model": model})
    log(f"Removing {provider} {model}")
    try:
        req = urllib.request.Request(url, method="DELETE")
        with urllib.request.urlopen(req, timeout=min(10, remaining)) as response:
            result = json.load(response)
        if not isinstance(result, dict) or result.get("success") is not True:
            raise ValueError("response did not report success")
        log(f"Removed {provider} {model}")
    except (OSError, ValueError) as exc:
        if time.monotonic() >= deadline:
            raise Deadline() from exc
        log(f"Removal failed for {provider} {model}: {exc}", True)
    if time.monotonic() >= deadline:
        raise Deadline()


def main():
    """Parse settings and run one feature until stop or failure."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=positive, help="per-Pi-call timeout in minutes")
    parser.add_argument("--hours", type=positive, help="overall run limit in hours")
    parser.add_argument("--remove", action="store_true", help="remove previously disqualified llama.cpp models")
    args = parser.parse_args()
    if CWD.resolve() == ROOT:
        parser.error("run from a different directory than runner.py")
    locked = False
    try:
        acquire_lock()
        locked = True
        state = load()
        settings = state["settings"]
        settings["timeout"] = args.timeout if args.timeout is not None else settings.get("timeout", 20)
        settings["hours"] = args.hours if args.hours is not None else settings.get("hours", 1)
        deadline = START + settings["hours"] * 3600
        save(state)
        if time.monotonic() >= deadline:
            raise Deadline()
        try:
            (CWD / "task/vision.md").read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise Failure(f"Cannot read task/vision.md: {exc}") from exc
        prepare_prompts()
        if args.remove:
            for item in state["disqualified"]:
                remove_model((item["provider"], item["model"]), deadline)
        leader = None
        while True:
            team = discover(state, deadline, settings["timeout"] * 60)
            if leader not in team:
                leader = random.choice(sorted(team))
                log(f"Selected leader: {leader[0]} {leader[1]}", True)
            (CWD / "next.txt").unlink(missing_ok=True)
            before = {name: digest(name) for name in ("assignment.md", "report.md", "notes.md", "suggestion.md")}
            log(f"Calling leader {leader[0]} {leader[1]}", True)
            _, stderr, pi_errors, code, timed = run_pi(["--provider", leader[0], "--model", leader[1], "--mode", "json", "-p", "@prompts/leader.md"], deadline, settings["timeout"] * 60, True)
            changed = {name: digest(name) != value for name, value in before.items()}
            for name in ("assignment.md", "report.md", "notes.md"):
                log(f"Leader {name} changed: {changed[name]}")
            if not changed["notes.md"]:
                event(state, leader, "leader", "missed-notes")
            choice = handover(team)
            error = "timed out" if timed else (pi_errors[0] if pi_errors else (("exit code " + str(code) + (": " + " ".join(stderr) if stderr else "")) if code else ""))
            if error and choice:
                log(f"Leader call failed but produced a valid handover: {error}", True)
            if not choice:
                reason = error or "invalid or missing next.txt"
                disqualify(state, leader, "leader", reason)
                if "500" in reason and "failed to load" in reason.lower():
                    remove_model(leader, deadline)
                leader = None
                continue
            action, pair = choice
            if action == "stop":
                if not changed["suggestion.md"]:
                    event(state, leader, "leader", "missed-suggestion")
                log("Leader stopped the run", True)
                return 0
            if action == "disqualify":
                disqualify(state, pair, "leader-selected", "disqualified by leader; see notes.md")
                if pair == leader:
                    leader = None
                continue
            if not changed["assignment.md"]:
                event(state, leader, "leader", "missed-assignment")
            if pair == leader:
                log("Leader assigned its own model as worker")
            before = {name: digest(name) for name in ("assignment.md", "report.md")}
            log(f"Calling worker {pair[0]} {pair[1]} with {action}", True)
            _, stderr, pi_errors, code, timed = run_pi(["--provider", pair[0], "--model", pair[1], "--mode", "json", "-p", "@" + action], deadline, settings["timeout"] * 60, True)
            changed = {name: digest(name) != value for name, value in before.items()}
            for name in changed:
                log(f"Worker {name} changed: {changed[name]}")
            if not changed["report.md"]:
                event(state, pair, "worker", "missed-report")
            error = "timed out" if timed else (pi_errors[0] if pi_errors else (("exit code " + str(code) + (": " + " ".join(stderr) if stderr else "")) if code else ""))
            if error and changed["report.md"]:
                log(f"Worker call failed but changed report.md: {error}", True)
            elif error:
                disqualify(state, pair, "worker", error)
                if "500" in error and "failed to load" in error.lower():
                    remove_model(pair, deadline)
                if pair == leader:
                    leader = None
    except Deadline:
        log("Run reached its time limit", True)
        return 3
    except KeyboardInterrupt:
        log("interrupted", True)
        return 130
    except (Failure, OSError) as exc:
        try:
            log(f"Error: {exc}", True)
        except OSError:
            print(f"Error: {exc}", file=sys.stderr)
        return exc.code if isinstance(exc, Failure) else 1
    finally:
        if locked:
            (CWD / "runner.lock").unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
