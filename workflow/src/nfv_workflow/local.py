# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Local, asynchronous orchestration of the existing NFV review/benchmark engine."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid

from .github import Abstain, collect, collect_sources, repository, sha, source_path

TERMINAL = {"completed", "failed", "cancelled"}
TOKEN_VARIABLES = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
_NATIVE_CHILDREN = {}


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8", newline="\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def load(path):
    from .cli import load as read
    return read(path)


def diagnostic(exc):
    message = str(exc)
    for key in TOKEN_VARIABLES:
        if os.environ.get(key):
            message = message.replace(os.environ[key], "[REDACTED]")
    return message[:3000]


def command(args, *, cwd=None, env=None, timeout=60):
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, timeout=timeout)
    if result.returncode:
        raise Abstain(diagnostic(result.stderr.decode("utf-8", errors="replace").strip()
                                 or f"{args[0]} exited with {result.returncode}"))
    if len(result.stdout) > 12_000_000:
        raise Abstain("Command output exceeds the 12 MB input budget")
    return result.stdout


class LocalGit:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()

    def git(self, *args):
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        return command(["git", "--no-pager", "--no-replace-objects", "--literal-pathspecs",
                        "-c", "core.fsmonitor=false", "-C", str(self.directory), *args], env=env)

    def revision(self, ref):
        if not ref or ref.startswith("-"):
            raise Abstain("Use a commit or revision name, not a Git option")
        return sha(self.git("rev-parse", "--verify", "--end-of-options", ref + "^{commit}")
                   .decode().strip())

    def tree(self, commit):
        rows = []
        for entry in self.git("ls-tree", "-r", "-l", "-z", sha(commit)).split(b"\0"):
            if not entry:
                continue
            metadata, path = entry.split(b"\t", 1)
            mode, kind, oid, size = metadata.decode().split()
            rows.append({"path": source_path(path.decode("utf-8")), "mode": mode,
                         "type": kind, "sha": oid,
                         "size": int(size) if kind == "blob" else 0})
        return {"tree": rows, "truncated": False}

    def request(self, path):
        match = re.fullmatch(r"repos/[\w.-]+/[\w.-]+/git/trees/([a-f0-9]{40})\?recursive=1", path)
        if not match:
            raise Abstain("Local source capture only supports immutable Git trees")
        return self.tree(match[1])

    def blob(self, repo, oid):
        repository(repo)
        oid = sha(oid)
        if int(self.git("cat-file", "-s", oid)) > 1_000_000:
            raise Abstain("Source blob unsupported or too large")
        raw = self.git("cat-file", "blob", oid)
        actual = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        if actual != oid:
            raise Abstain("Source blob hash mismatch")
        return raw.decode("utf-8")


def capture(directory, base, head, intent, repo="local/repository"):
    if not isinstance(intent, str) or not intent.strip() or len(intent) > 12000:
        raise Abstain("Supply nonempty explicit intent of at most 12,000 characters")
    api = LocalGit(directory)
    base, head = api.revision(base), api.revision(head)
    # Compare committed trees only; --no-textconv/--no-ext-diff prevent source-owned commands.
    flags = ("--no-ext-diff", "--no-textconv", "--no-renames")
    names = api.git("diff", *flags, "--name-status", "-z", base, head).split(b"\0")
    files = []
    for index in range(0, len(names) - 1, 2):
        status = names[index].decode()
        path = source_path(names[index + 1].decode("utf-8"))
        if status not in ("A", "M", "D", "T"):
            raise Abstain("Unsupported committed change status")
        patch = api.git("diff", *flags, "--unified=0", base, head, "--", path).decode("utf-8")
        files.append({"filename": path, "status": "removed" if status == "D" else "modified",
                      "patch": patch})
    if not files or len(files) > 300:
        raise Abstain("Select a nonempty committed change within the 300-file budget")
    ident = {"repository": repository(repo), "number": 0, "source_kind": "local-change",
             "source_sha": head, "head_sha": head, "base_sha": base}
    bundle = {"schema": 1, "identity": ident,
              "signals": [{"id": "local-intent", "kind": "explicit-intent", "text": intent,
                           "text_sha256": hashlib.sha256(intent.encode("utf-8")).hexdigest(),
                           "truncated": False, "url": ""}],
              **collect_sources(ident, files, api)}
    return bundle, api


def preflight():
    from nfv.shared.config import DAFNY_PATH, Z3_PATH
    if not DAFNY_PATH or not Z3_PATH:
        raise Abstain("Install Dafny 4.11.0 and bundled Z3 4.12.1 before local inference")
    for tool, expected in ((DAFNY_PATH, "4.11.0"), (Z3_PATH, "4.12.1")):
        result = command([tool, "--version"]).decode()
        if not re.search(r"(?<![\d.])" + re.escape(expected) + r"(?![\d.])", result):
            raise Abstain(f"Local verifier must use version {expected}")
    if version("github-copilot-sdk") != "1.0.4":
        raise Abstain("Install this release's github-copilot-sdk==1.0.4")


def process_module():
    try:
        import psutil
    except ImportError as exc:
        raise Abstain("Install the workflow's [local] extra for local process supervision") from exc
    return psutil


def owned_process(runtime):
    psutil = process_module()
    try:
        process = psutil.Process(runtime["pid"])
        if process.create_time() != runtime["created_at"] or process.status() == psutil.STATUS_ZOMBIE:
            return None
        return process
    except psutil.NoSuchProcess:
        return None
    except psutil.AccessDenied as exc:
        raise Abstain("Cannot inspect the recorded worker process; check local process permissions") from exc


def stop_process(process):
    psutil = process_module()
    try:
        process.suspend()
        children = process.children(recursive=True)
        try:
            for child in reversed(children):
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
        finally:
            process.kill()
        _, alive = psutil.wait_procs([*children, process], timeout=10)
        if alive:
            raise Abstain("Worker termination did not finish; inspect the recorded process IDs")
    except psutil.NoSuchProcess:
        return
    except psutil.AccessDenied as exc:
        raise Abstain("Cannot terminate the recorded worker process tree; check local process permissions") from exc


def run_directory(root, run_id):
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise Abstain("Invalid local run ID")
    directory = Path(root).resolve() / run_id
    if directory.is_symlink() or not directory.is_dir():
        raise Abstain("Local run directory is unavailable")
    return directory


def state(directory, status, phase, **extra):
    value = {"run_id": directory.name, "status": status, "phase": phase, "updated_at": now(), **extra}
    save(directory / "status.json", value)
    return value


def start(args):
    from .benchmark import prepare
    from .general import Limits
    from .source_context import function_catalog

    if not args.authorize_inference:
        raise Abstain("Starting a review or benchmark requires explicit --authorize-inference")
    if (not args.model or args.model != args.model.strip() or len(args.model) > 200
            or any(ord(c) < 32 for c in args.model)):
        raise Abstain("Specify the authorized model")
    if args.timeout_seconds < 1:
        raise Abstain("Run timeout must be positive")
    if not 0 <= args.syntax_rounds <= 6 or not 0 <= args.proof_rounds <= 10:
        raise Abstain("Local repair limits are syntax 0..6 and proof 0..10")
    limits = Limits(syntax_rounds=args.syntax_rounds, proof_rounds=args.proof_rounds,
                    verifier_timeout=args.verifier_timeout, entry_seconds=args.property_seconds,
                    minimize_assumptions=not args.benchmark)
    if args.runtime == "native":
        process_module()
        preflight()
        image = None
    else:
        image = json.loads(command(["docker", "image", "inspect", args.image]))[0]["Id"]
    if args.benchmark:
        if args.event or args.base or args.head or args.intent_file or args.context_manifest:
            raise Abstain("Benchmark mode uses the frozen supplied specifications, not change/intent options")
        if not args.dataset or not args.release_sha:
            raise Abstain("Benchmark mode requires --dataset and the trusted NFV --release-sha")
        bundle = None
    else:
        if args.dataset or args.release_sha:
            raise Abstain("Dataset and release options require --benchmark")
        if args.event:
            if args.base or args.head:
                raise Abstain("Choose a PR event or local base/head commits, not both")
            from .github import GitHub
            api = GitHub()
            bundle = collect(load(args.event), api, event_snapshot=True)
            if args.intent_file:
                intent = read_intent(args.intent_file)
                bundle["signals"].append({"id": "local-intent", "kind": "explicit-intent",
                                          "text": intent, "url": ""})
        else:
            if not args.base or not args.head or not args.intent_file:
                raise Abstain("Local changes require --base, --head and --intent-file")
            bundle, api = capture(args.repo_path, args.base, args.head,
                                  read_intent(args.intent_file), args.repository)
        if args.context_manifest:
            from .repository_context import enrich
            hints = load(args.context_manifest)
            if not isinstance(hints, dict) or set(hints) != {"files"}:
                raise Abstain("Context hints must contain only a files list of paths and symbols")
            bundle = enrich(bundle, {"identity": bundle["identity"], **hints}, api)
        if len(json.dumps(bundle).encode("utf-8")) > 12_000_000:
            raise Abstain("Captured bundle exceeds its 12 MB input budget")
        catalog = function_catalog(bundle)
        if not catalog["functions"]:
            raise Abstain("No inspectable changed functions: " + json.dumps(
                {"errors": catalog["errors"], "omissions": bundle.get("context_omissions", [])}))

    directory = Path(args.runs_dir).resolve() / uuid.uuid4().hex
    if args.runtime == "docker" and "," in str(directory):
        raise Abstain("Docker bind-mount paths cannot contain commas")
    directory.mkdir(parents=True)
    config = {"schema": 1, "run_id": directory.name, "kind": "benchmark" if args.benchmark else "review",
              "model": args.model, "limits": asdict(limits), "runtime": args.runtime, "image": image,
              "timeout_seconds": args.timeout_seconds, "authorized_at": now(),
              "simplify_refutations": args.simplify_refutations, "check_fixes": args.check_fixes}
    try:
        if args.benchmark:
            prepare(args.dataset, directory / "prepared", args.release_sha, args.model,
                    args.repository, time.time_ns(), 1, limits, count=args.entries, execution="local")
        else:
            save(directory / "inputs" / "bundle.json", bundle)
            config["bundle_sha256"] = hashlib.sha256(
                (directory / "inputs" / "bundle.json").read_bytes()).hexdigest()
        save(directory / "run.json", config)
        state(directory, "running", "starting")
        launch(directory, config)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        state(directory, "failed", "launch", diagnostic=diagnostic(exc))
        raise Abstain(f"Local run {directory.name} failed to start: {diagnostic(exc)}") from exc
    return {"run_id": directory.name, "status": "started", "directory": str(directory),
            "runtime": args.runtime, "log": str(directory / "worker.log")}


def read_intent(path):
    path = Path(path)
    if path.stat().st_size > 48000:
        raise Abstain("Intent exceeds the 12,000-character budget")
    text = path.read_text(encoding="utf-8")
    if not text.strip() or len(text) > 12000:
        raise Abstain("Supply nonempty explicit intent of at most 12,000 characters")
    return text


def launch(directory, config):
    if config["runtime"] == "docker":
        args = ["docker", "run", "--detach", "--name", "nfv-local-" + directory.name,
                "--label", "nfv.local.run=" + directory.name,
                "--mount", f"type=bind,source={directory},target=/run/nfv",
                "--workdir", "/run/nfv", "--entrypoint", "python"]
        input_name = "prepared" if config["kind"] == "benchmark" else "inputs"
        args.extend(["--mount", f"type=bind,source={directory / input_name},target=/run/nfv/{input_name},readonly",
                     "--mount", f"type=bind,source={directory / 'run.json'},target=/run/nfv/run.json,readonly"])
        for key in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "NFV_EXPECTED_LOGIN", "NFV_ALLOW_EMU"):
            if os.environ.get(key):
                args.extend(["--env", key])
        args.extend([config["image"], "-m", "nfv_workflow.local", "_worker", "--run-dir", "/run/nfv"])
        container = command(args).decode().strip()
        try:
            save(directory / "runtime.json", {"kind": "docker", "container": container})
        except OSError:
            command(["docker", "stop", "--time", "30", container])
            raise
    else:
        import nfv
        psutil = process_module()
        env = dict(os.environ)
        for key in ("GITHUB_ACTIONS", "GITHUB_AW", "COPILOT_SDK_URI", "COPILOT_CONNECTION_TOKEN",
                    "COPILOT_PROVIDER_BASE_URL", "COPILOT_PROVIDER_TYPE"):
            env.pop(key, None)
        # Only trusted NFV package locations enter the worker's import path, never the subject checkout.
        env["PYTHONPATH"] = os.pathsep.join(
            (str(Path(__file__).resolve().parents[1]), str(Path(nfv.__file__).resolve().parents[1])))
        options = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} \
            if os.name == "nt" else {"start_new_session": True}
        with (directory / "launcher.log").open("ab") as log:
            process = subprocess.Popen([sys.executable, "-P", "-m", "nfv_workflow.local",
                                        "_worker", "--run-dir", str(directory)],
                                       cwd=directory, env=env, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=log, **options)
        try:
            created = psutil.Process(process.pid).create_time()
        except psutil.NoSuchProcess as exc:
            process.wait()
            raise Abstain("Worker exited during launch; inspect launcher.log") from exc
        _NATIVE_CHILDREN[process.pid] = process
        try:
            save(directory / "runtime.json", {"kind": "native", "pid": process.pid, "created_at": created})
        except OSError:
            stop_process(psutil.Process(process.pid))
            _NATIVE_CHILDREN.pop(process.pid)
            process.wait(timeout=10)
            raise


def status(directory):
    value = load(directory / "status.json")
    if value["status"] not in TERMINAL:
        runtime = load(directory / "runtime.json")
        if runtime["kind"] == "native":
            child = _NATIVE_CHILDREN.get(runtime["pid"])
            if child is not None and child.poll() is not None:
                del _NATIVE_CHILDREN[runtime["pid"]]
            alive = owned_process(runtime) is not None
        elif runtime["kind"] == "docker":
            record = json.loads(command(["docker", "inspect", runtime["container"]]))[0]
            if record["Config"]["Labels"].get("nfv.local.run") != directory.name:
                raise Abstain("Container does not belong to this local run")
            alive = record["State"]["Running"]
        else:
            raise Abstain("Unknown local runtime")
        if not alive:
            # Re-read after observing exit: the worker may have just written its final checkpoint.
            value = load(directory / "status.json")
            if value["status"] not in TERMINAL:
                value = state(directory, "failed", value["phase"],
                              diagnostic="Worker exited without a completion checkpoint; inspect worker/launcher logs")
    return {**value, "directory": str(directory), "log": str(directory / "worker.log")}


def result(directory):
    value = status(directory)
    if value["status"] != "completed":
        raise Abstain(f"Run is {value['status']}; inspect status and logs, not a final report")
    config = load(directory / "run.json")
    if config["kind"] == "review":
        evidence = directory / "evidence"
        report = load(evidence / "report.json")
        target = evidence / "comment.md"
    else:
        evidence = directory / "benchmark-evidence"
        report = load(directory / "summary" / "summary.json")
        target = directory / "summary" / "report.md"
    if report.get("completed") is not True or report.get("errors") or not target.is_file():
        raise Abstain("Final report is missing or incomplete")
    return {**value, "report": str(target), "evidence": str(evidence)}


def cancel(directory):
    value = status(directory)
    if value["status"] in TERMINAL:
        return value
    runtime = load(directory / "runtime.json")
    if runtime["kind"] == "docker":
        command(["docker", "stop", "--time", "30", runtime["container"]])
    else:
        process = owned_process(runtime)
        if process is not None:
            stop_process(process)
        child = _NATIVE_CHILDREN.pop(runtime["pid"], None)
        if child is not None:
            child.wait(timeout=10)
    value = load(directory / "status.json")
    if value["status"] in TERMINAL:
        return value
    return state(directory, "cancelled", value["phase"],
                 diagnostic="Cancelled by request; interrupted evidence is not a final report")


def execute(directory, config):
    from .cli import audit, propose
    from .followups import Options
    from .general import Limits

    preflight()
    os.environ["NFV_MODEL"] = config["model"]
    limits = Limits(**config["limits"])
    if config["kind"] == "review":
        evidence = directory / "evidence"
        input_path = directory / "inputs" / "bundle.json"
        if hashlib.sha256(input_path.read_bytes()).hexdigest() != config["bundle_sha256"]:
            raise Abstain("Captured local review input changed after launch")
        save(evidence / "bundle.json", load(input_path))
        state(directory, "running", "extracting")
        proposals = asyncio.run(propose(load(evidence / "bundle.json"), config["model"],
                                        intent_only=True, output=evidence))
        save(evidence / "proposals.json", proposals)
        state(directory, "running", "analyzing")
        os.environ.update(NFV_BUNDLE=str(evidence / "bundle.json"), NFV_OUT=str(evidence))
        audit(evidence / "proposals.json", limits,
              Options(config["simplify_refutations"], config["check_fixes"]), local=True)
    elif config["kind"] == "benchmark":
        from .benchmark import prepared_cases, run_cases, summarize
        manifest, _ = prepared_cases(directory / "prepared")
        if (manifest["run"].get("execution") != "local" or manifest["run"]["model"] != config["model"]
                or manifest["run"]["limits"] != config["limits"]):
            raise Abstain("Local benchmark configuration changed after preparation")
        for shard in range(manifest["run"]["shards"]):
            state(directory, "running", "benchmark", shard=shard, shards=manifest["run"]["shards"])
            run_cases(directory / "prepared" / "shards" / f"{shard}.json",
                      directory / "benchmark-evidence" / str(shard))
        summary = summarize(directory / "prepared", directory / "benchmark-evidence", directory / "summary")
        if summary.get("completed") is not True:
            raise Abstain("Local benchmark did not complete; inspect the partial summary")
    else:
        raise Abstain("Unknown local task kind")


def worker(directory):
    from contextlib import redirect_stderr, redirect_stdout

    config = load(directory / "run.json")
    finished = threading.Event()
    lock = threading.Lock()

    def deadline():
        if finished.wait(config["timeout_seconds"]):
            return
        with lock:
            if finished.is_set():
                return
            try:
                state(directory, "failed", "timeout", diagnostic="Local run exceeded its wall-clock budget")
                process = process_module().Process()
                for child in process.children(recursive=True):
                    try:
                        child.kill()
                    except process_module().NoSuchProcess:
                        pass
            finally:
                os._exit(124)

    threading.Thread(target=deadline, daemon=True).start()
    with (directory / "worker.log").open("a", encoding="utf-8", buffering=1) as log, \
            redirect_stdout(log), redirect_stderr(log):
        try:
            execute(directory, config)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, ImportError,
                subprocess.SubprocessError) as exc:
            detail = diagnostic(exc)
            print(detail, flush=True)
            extra = {}
            if config["kind"] == "benchmark":
                from .benchmark import summarize
                try:
                    summarize(directory / "prepared", directory / "benchmark-evidence",
                              directory / "summary", workers_succeeded=False)
                except (OSError, ValueError, RuntimeError, KeyError, TypeError) as summary_error:
                    extra["summary_diagnostic"] = diagnostic(summary_error)
            with lock:
                finished.set()
                state(directory, "failed", "error", diagnostic=detail, **extra)
            return 1
        else:
            with lock:
                finished.set()
                state(directory, "completed", "finished")
            return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local NFV skill runtime; start is asynchronous and billable")
    parser.add_argument("--runs-dir", default=str(Path.cwd() / ".nfv-work" / "runs"))
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("start", help="Capture inputs and start an asynchronous review or benchmark")
    p.add_argument("--runtime", choices=("native", "docker"), default="native")
    p.add_argument("--image", default="nfv-local:latest", help="Trusted prebuilt image; resolved to its image ID")
    p.add_argument("--model", required=True)
    p.add_argument("--authorize-inference", action="store_true")
    p.add_argument("--repo-path", default=".")
    p.add_argument("--repository", default="local/repository", help="Source repository label; no publication")
    p.add_argument("--base")
    p.add_argument("--head")
    p.add_argument("--intent-file")
    p.add_argument("--event", help="PR event JSON; collect immutable GitHub source through read-only API")
    p.add_argument("--context-manifest", help="Optional captured-source hints: files with path/symbols only")
    p.add_argument("--benchmark", action="store_true")
    p.add_argument("--dataset")
    p.add_argument("--release-sha")
    p.add_argument("--entries", type=int, choices=(5, 206), default=5)
    p.add_argument("--syntax-rounds", type=int, default=6)
    p.add_argument("--proof-rounds", type=int, default=10)
    p.add_argument("--verifier-timeout", type=int, default=60)
    p.add_argument("--property-seconds", type=int, default=1800)
    p.add_argument("--timeout-seconds", type=int, default=21600)
    p.add_argument("--simplify-refutations", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--check-fixes", action=argparse.BooleanOptionalAction, default=True)
    for name in ("status", "result", "cancel"):
        commands.add_parser(name).add_argument("run_id")
    commands.add_parser("_worker", help="Internal worker; use start to launch").add_argument("--run-dir", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "_worker":
            return worker(Path(args.run_dir).resolve())
        if args.command == "start":
            value = start(args)
        else:
            directory = run_directory(args.runs_dir, args.run_id)
            value = {"status": status, "result": result, "cancel": cancel}[args.command](directory)
        print(json.dumps(value))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, ImportError,
            subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "error", "message": diagnostic(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
