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


class Interrupted(Exception):
    """The runner received SIGTERM or SIGHUP; handled like Ctrl+C."""

    def __init__(self, signum):
        super().__init__(signal.Signals(signum).name)
        self.signum = signum


def on_signal(signum, _frame):
    """Turn SIGTERM/SIGHUP into an exception so cleanup runs and Pi is terminated."""
    raise Interrupted(signum)


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
            except (ValueError, OSError):
                # empty or garbage lock, e.g. from a crash right after creating it
                log("Warning: removing unreadable lock", True)
                path.unlink(missing_ok=True)
                continue
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                log(f"Warning: removing stale lock for PID {pid}", True)
                path.unlink(missing_ok=True)
                continue
            except OSError as exc:
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


def target(args):
    """Return a short description of a tool call's target: path or command if present, else the arguments."""
    if isinstance(args, dict):
        for key in ("path", "file_path", "command"):
            if isinstance(args.get(key), str):
                return args[key][:120]
    return json.dumps(args, ensure_ascii=False)[:120] if args else ""


def result_text(result):
    """Extract a short single-line error text from a Pi tool result."""
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        text = " ".join(str(c.get("text", "")) for c in result["content"] if isinstance(c, dict))
    elif isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, ensure_ascii=False)
    return " ".join(text.split())[:200]


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
    tools = [0]  # number of tool calls started; 0 means the model only produced text
    started = {}  # tool call id -> shortened arguments, to name the target of a failed call

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
        elif kind_event in ("tool_execution_start", "tool_execution_end"):
            # tool_execution_update events are ignored: they repeat during long tools
            tool = obj.get("toolName", "tool")
            call_id = obj.get("toolCallId") or obj.get("id")
            if kind_event == "tool_execution_start":
                tools[0] += 1
                detail = json.dumps(obj.get("args", {}), ensure_ascii=False)[:200]
                if call_id:
                    started[call_id] = target(obj.get("args"))
                msg = f"Tool start: {tool} {detail}"
            else:
                what = started.pop(call_id, "") if call_id else ""  # without an id the start cannot be matched
                if not obj.get("isError"):
                    return  # successful tool ends carry no information
                msg = f"Tool error: {tool} {what}: {result_text(obj.get('result'))}".replace("  ", " ")
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
        if stream:
            print(flush=True)  # end streamed model text so runner messages start on a new line
    log(f"Pi exit code: {proc.returncode}" + (" (timed out)" if timed_out else ""))
    return output, errors, pi_errors, proc.returncode, timed_out, tools[0]


def discover(state, deadline, timeout):
    """Refresh team.txt from Pi's model table, excluding disqualified models."""
    out, err, _, code, timed, _ = run_pi(["--list-models"], deadline, timeout, False)
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
    if not rows:
        (CWD / "team.txt").write_text("", encoding="utf-8")
        raise Failure("No eligible models available", 4)
    ids = model_ids(rows)
    header = out[0] if out else "provider model"
    width = len(next(iter(ids)))
    # header row labels the columns; each row starts with its model id
    lines = [f"{'id':<{width}}  {header}"] + [f"{mid}  {rows[pair]}" for mid, pair in ids.items()]
    (CWD / "team.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return ids


def model_ids(pairs):
    """Map short stable ids to (provider, model) pairs.

    The id is the first 6 lowercase hex characters of sha256("<provider> <model>");
    if any two pairs collide, 8 characters are used for all of them.
    """
    for length in (6, 8):
        ids = {hashlib.sha256(f"{p} {m}".encode()).hexdigest()[:length]: (p, m) for p, m in pairs}
        if len(ids) == len(pairs):
            return ids
    raise Failure("Model id collision even with 8 characters")


def handover(team):
    """Parse and validate the leader's handover against the current team.

    ``team`` maps model ids to (provider, model). Returns ``(choice, reason)``:
    ``choice`` is ``(action, pair)`` for a valid handover, otherwise ``None``
    and ``reason`` explains the rejection. The raw file content is logged.
    """
    try:
        text = (CWD / "next.txt").read_text(encoding="utf-8")
    except FileNotFoundError:
        if (CWD / "prompts" / "next.txt").is_file():
            return None, "next.txt not written; found prompts/next.txt instead (written in the wrong directory)"
        return None, "next.txt not written"
    except (OSError, UnicodeError) as exc:
        return None, f"cannot read next.txt: {exc}"
    log("next.txt content: " + json.dumps(text, ensure_ascii=False))
    lines = [line.strip() for line in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()  # trailing blank lines are accepted (design: Handover)
    if lines == ["stop"]:
        return ("stop", None), ""
    if len(lines) != 2 or not all(lines):
        return None, f"next.txt must have 1 ('stop') or 2 non-empty lines (prompt or 'disqualify', model id), got {len(lines)}"
    action, raw_id = lines
    mid = parse_id(raw_id)
    if mid not in team:
        return None, f"next.txt names model id {raw_id!r}, which is not in team.txt"
    pair = team[mid]
    if action == "disqualify":
        return (action, pair), ""
    path = Path(action)
    if len(path.parts) != 2 or path.parts[0] != "prompts" or path.name in ("leader.md", ".", "..") or path.suffix != ".md":
        return None, f"next.txt names invalid worker prompt {action!r}"
    if not (CWD / path).is_file():
        return None, f"next.txt names missing worker prompt {action}"
    return (action, pair), ""


def parse_id(text):
    """Normalize a model id from next.txt: lowercase, strip quotes/backticks and a label like 'id:' or 'model ='."""
    value = text.strip().strip("`'\"").strip().lower()
    for label in ("id", "model"):
        if value.startswith(label) and value[len(label):len(label) + 1] in (" ", ":", "="):
            value = value[len(label):].lstrip(" :=")
    return value.strip("`'\"").strip()


def server_base(provider):
    """Return the llama.cpp server base URL for a `llama-server=<url>` provider (trailing /v1 stripped), else None."""
    if not provider.startswith("llama-server="):
        return None
    base = provider[len("llama-server="):].rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


def not_running(pi_errors):
    """True if Pi reported that the llama.cpp router has the model unloaded."""
    return any("is not running" in e for e in pi_errors)


def load_failure(error):
    """True if an error text indicates the model could not be loaded (triggers removal)."""
    return ("500" in error and "failed to load" in error.lower()) or "is not running" in error


LOAD_WAIT = 600  # max seconds to wait for a model load (slow disks, e.g. USB)
LOAD_POLL = 10  # seconds between status checks while waiting


def model_status(base, model, deadline):
    """Return the router's status string for a model from GET /models, or "" if unknown.

    Accepts ``{"data": [...]}`` or a plain list; an entry matches on ``id``,
    ``name`` or ``model``; ``status`` may be a string or ``{"value": ...}``.
    """
    try:
        timeout = max(0.1, min(10, deadline - time.monotonic()))
        with urllib.request.urlopen(base + "/models", timeout=timeout) as response:
            data = json.load(response)
    except (OSError, ValueError) as exc:
        log(f"Model status check failed: {exc}")
        return ""
    entries = data.get("data", []) if isinstance(data, dict) else data
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and model in (entry.get("id"), entry.get("name"), entry.get("model")):
            status = entry.get("status")
            if isinstance(status, dict):
                status = status.get("value")
            return str(status or "").lower()
    return ""


def load_model(pair, deadline):
    """Ask the llama.cpp router to load a model and wait until it reports loaded.

    Polls GET /models every LOAD_POLL seconds for up to LOAD_WAIT seconds (bounded
    by the run deadline); stops early on a failed status. Failures of the load
    request are logged only; the caller retries the call anyway.
    """
    provider, model = pair
    base = server_base(provider)
    if not base:
        return
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Deadline()
    log(f"Model not running, requesting load: {provider} {model}", True)
    try:
        body = json.dumps({"model": model}).encode()
        req = urllib.request.Request(base + "/models/load", data=body, method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=min(10, remaining)) as response:
            log(f"Load request answered: {response.read(500).decode('utf-8', 'replace')}")
    except (OSError, ValueError) as exc:
        log(f"Load request failed for {provider} {model}: {exc}", True)
    started = time.monotonic()
    status = ""
    while time.monotonic() - started < LOAD_WAIT:
        if time.monotonic() >= deadline:
            raise Deadline()
        status = model_status(base, model, deadline)
        if status == "loaded" or "fail" in status or "error" in status:
            break
        time.sleep(max(0, min(LOAD_POLL, deadline - time.monotonic())))
    log(f"Model status after {time.monotonic() - started:.0f} s: {status or 'unknown'}", True)
    if time.monotonic() >= deadline:
        raise Deadline()


def call_model(pair, prompt, deadline, timeout):
    """Run a leader/worker Pi call; if the model is not running, load it and retry once."""
    args = ["--provider", pair[0], "--model", pair[1], "--mode", "json", "-p", "@" + prompt]
    result = run_pi(args, deadline, timeout, True)
    if not_running(result[2]):
        load_model(pair, deadline)
        log(f"Retrying call for {pair[0]} {pair[1]}", True)
        result = run_pi(args, deadline, timeout, True)
    return result


def remove_model(pair, deadline):
    """Request model removal from a llama.cpp provider if applicable."""
    provider, model = pair
    base = server_base(provider)
    if not base:
        log(f"Skipping removal for non-llama provider {provider} {model}")
        return
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Deadline()
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
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, on_signal)
    locked = False
    try:
        # clear marker in runner.log where a new invocation begins
        log("=" * 20 + f" RUN START pid={os.getpid()} cwd={CWD} args={shlex.join(sys.argv[1:]) or '(none)'} " + "=" * 20, True)
        acquire_lock()
        locked = True
        state = load()
        settings = state["settings"]
        settings["timeout"] = args.timeout if args.timeout is not None else settings.get("timeout", 20)
        settings["hours"] = args.hours if args.hours is not None else settings.get("hours", 1)
        deadline = START + settings["hours"] * 3600
        save(state)
        log(f"Settings: timeout={settings['timeout']} min, hours={settings['hours']}, disqualified={len(state['disqualified'])}", True)
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
            if leader not in team.values():
                leader = random.choice(sorted(team.values()))
                log(f"Selected leader: {leader[0]} {leader[1]}", True)
            (CWD / "next.txt").unlink(missing_ok=True)
            (CWD / "prompts" / "next.txt").unlink(missing_ok=True)  # stale misplaced handover; see handover()
            before = {name: digest(name) for name in ("assignment.md", "report.md", "notes.md", "suggestion.md")}
            log(f"Calling leader {leader[0]} {leader[1]}", True)
            _, stderr, pi_errors, code, timed, _ = call_model(leader, "prompts/leader.md", deadline, settings["timeout"] * 60)
            changed = {name: digest(name) != value for name, value in before.items()}
            for name in ("assignment.md", "report.md", "notes.md"):
                log(f"Leader {name} changed: {changed[name]}")
            if not changed["notes.md"]:
                event(state, leader, "leader", "missed-notes")
            choice, rejection = handover(team)
            error = "timed out" if timed else (pi_errors[0] if pi_errors else (("exit code " + str(code) + (": " + " ".join(stderr) if stderr else "")) if code else ""))
            if error and choice:
                log(f"Leader call failed but produced a valid handover: {error}", True)
            if not choice:
                reason = error or rejection
                disqualify(state, leader, "leader", reason)
                if load_failure(reason):
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
                disqualify(state, pair, "leader-selected", f"disqualified by leader {leader[0]} {leader[1]}; see notes.md")
                if pair == leader:
                    leader = None
                continue
            if not changed["assignment.md"]:
                event(state, leader, "leader", "missed-assignment")
            if pair == leader:
                log("Leader assigned its own model as worker")
            before = {name: digest(name) for name in ("assignment.md", "report.md")}
            log(f"Calling worker {pair[0]} {pair[1]} with {action}", True)
            _, stderr, pi_errors, code, timed, tools = call_model(pair, action, deadline, settings["timeout"] * 60)
            changed = {name: digest(name) != value for name, value in before.items()}
            for name in changed:
                log(f"Worker {name} changed: {changed[name]}")
            if not changed["report.md"]:
                event(state, pair, "worker", "missed-report")
            error = "timed out" if timed else (pi_errors[0] if pi_errors else (("exit code " + str(code) + (": " + " ".join(stderr) if stderr else "")) if code else ""))
            if not error and not tools and not changed["report.md"]:
                error = "did no work (no tool call, report.md unchanged)"  # e.g. a model that only answers in text
            if error and changed["report.md"]:
                log(f"Worker call failed but changed report.md: {error}", True)
            elif error:
                disqualify(state, pair, "worker", error)
                if load_failure(error):
                    remove_model(pair, deadline)
                if pair == leader:
                    leader = None
    except Deadline:
        log("Run reached its time limit", True)
        return 3
    except KeyboardInterrupt:
        log("interrupted", True)
        return 130
    except Interrupted as exc:
        try:
            log(f"interrupted by {exc}", True)
        except OSError:  # terminal may be gone after SIGHUP
            log(f"interrupted by {exc}")
        return 128 + exc.signum
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
