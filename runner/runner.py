#!/usr/bin/env python3
"""Run alternating Pi leader and worker calls from the repository root."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
from tempfile import NamedTemporaryFile
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


# Anchor all control files and subprocesses to this checkout.
ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "runner"
WORK = ROOT / "work"
MEMORY = RUNNER / "memory.json"
TEAM = RUNNER / "team.txt"
NOTES = RUNNER / "notes.md"
NEXT = RUNNER / "next.txt"
ASSIGNMENT = WORK / "assignment.md"
REPORT = WORK / "report.md"
LEADER_PROMPT = "prompts/leader.md"
PI_TIMEOUT = 3600
DEFAULT_MAX_ROUNDS = 20
Model = tuple[str, str]  # Provider and exact model ID as listed by Pi.


def log(message):
    """Emit runner diagnostics separately from the leader's user-facing output."""
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


def run_pi(*arguments, cwd=ROOT):
    """Run Pi with a deadline and clean up its process group on interruption."""
    command = ["pi", *arguments]
    with subprocess.Popen(
        command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, encoding="utf-8", errors="replace", start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=PI_TIMEOUT)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
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
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def refresh_team(memory):
    """Refresh the team's model table and register any newly discovered models."""
    try:
        result = run_pi("--list-models")
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"pi --list-models timed out after {PI_TIMEOUT} seconds") from error
    if result.returncode:
        raise RuntimeError(f"pi --list-models exited with {result.returncode}: {result.stderr.strip()}")

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
        result = run_pi("--no-session", "--provider", provider, "--model", name, "-p", f"@{prompt_path}", cwd=cwd)
    except subprocess.TimeoutExpired:
        reason = f"Pi timed out after {PI_TIMEOUT} seconds"
    else:
        if result.returncode == 0:
            return result.stdout
        error_output = result.stderr.strip() or result.stdout.strip()
        reason = f"Pi exited with {result.returncode}"
        if error_output:
            reason += f": {error_output}"

    memory[model_key(model)].update(leader="disqualified", worker="failed", disqualification_reason=reason)
    save_memory(memory)
    log(f"Disqualified {model_key(model)}: {reason}")
    if error_output:
        delete_failed_model(model, error_output)
    return None


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
        output = run_model(leader, LEADER_PROMPT, memory)
        if output is None:
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
            if output.strip():
                print(output.rstrip(), flush=True)
            return 0

        preferred = leader
        prompt, worker = handoff
        REPORT.unlink(missing_ok=True)
        log(f"Round {round_number}/{max_rounds}, worker {model_key(worker)} using {prompt}")
        if run_model(worker, prompt, memory, cwd=WORK) is None:
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

    try:
        # Lock the runner directory without adding a persistent lock file.
        lock = os.open(RUNNER, os.O_RDONLY | os.O_DIRECTORY)
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("another runner is active in this workspace") from None
            return run_rounds(args.max_rounds, preferred)
        finally:
            os.close(lock)
    except KeyboardInterrupt:
        log("Runner interrupted.")
        return 130
    except (OSError, RuntimeError, ValueError) as error:
        log(f"Runner error: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
