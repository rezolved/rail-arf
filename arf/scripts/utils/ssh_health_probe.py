"""Shared SSH health probe primitives.

A single read-only probe of a remote GPU machine. Returns a structured
:class:`SshHealthReport` covering tmux sessions, GPU utilization and memory,
optional engine ``/health`` and ``/v1/completions`` reachability, and the
tail of an optional log file.

Used by:

* ``arf/skills/setup-remote-machine/`` — the engine smoke gate after VM
  provisioning (Lesson 2).
* ``arf/skills/diagnose-stuck-step/`` — the read-only diagnostic invoked when
  a step is flagged as ghosted or pathologically slow.

The probe accepts injectable ``ssh_runner`` and ``http_runner`` callables so
tests can stub the network entirely. When the callables are omitted, real
``subprocess.run`` (for SSH) and ``urllib.request`` (for HTTP) implementations
are used.

The probe is read-only: no remote mutation, no teardown authority.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

NVIDIA_SMI_UTILIZATION_CMD: str = (
    "nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits"
)
NVIDIA_SMI_MEMORY_CMD: str = "nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits"
TMUX_LIST_CMD: str = "tmux list-sessions"
TAIL_LINES: int = 200

HEALTH_ENDPOINT: str = "/health"
COMPLETIONS_ENDPOINT: str = "/v1/completions"
COMPLETIONS_PAYLOAD: str = json.dumps({"prompt": "ping", "max_tokens": 1})

HTTP_OK_LOWER: int = 200
HTTP_OK_UPPER: int = 300

DEFAULT_TIMEOUT_SECONDS: int = 30


@dataclass(frozen=True, slots=True)
class SshCommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class HttpResult:
    status_code: int
    body: str


@dataclass(frozen=True, slots=True)
class SshHealthReport:
    ssh_reachable: bool
    tmux_sessions: list[str]
    gpu_utilization_percent: int | None
    gpu_memory_used_mb: int | None
    engine_health_ok: bool | None
    engine_completion_ok: bool | None
    recent_log_tail: str | None
    error: str | None


def _default_ssh_runner(
    *,
    ssh_host: str,
    ssh_port: int,
    ssh_user: str,
    timeout_seconds: int,
) -> Callable[[list[str]], SshCommandResult]:
    def runner(args: list[str]) -> SshCommandResult:
        remote_cmd: str = " ".join(shlex.quote(arg) for arg in args)
        full_cmd: list[str] = [
            "ssh",
            "-p",
            str(ssh_port),
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={timeout_seconds}",
            f"{ssh_user}@{ssh_host}",
            remote_cmd,
        ]
        completed: subprocess.CompletedProcess[str] = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        return SshCommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    return runner


def _default_http_runner(
    *,
    timeout_seconds: int,
) -> Callable[[str], HttpResult]:
    def runner(url: str) -> HttpResult:
        try:
            request: urllib.request.Request = urllib.request.Request(url=url, method="GET")
            if url.endswith(COMPLETIONS_ENDPOINT):
                request = urllib.request.Request(
                    url=url,
                    data=COMPLETIONS_PAYLOAD.encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                body_bytes: bytes = response.read()
                return HttpResult(
                    status_code=int(response.status),
                    body=body_bytes.decode("utf-8", errors="replace"),
                )
        except urllib.error.HTTPError as http_err:
            return HttpResult(status_code=int(http_err.code), body=str(http_err))
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            return HttpResult(status_code=0, body=str(err))

    return runner


def _parse_tmux_sessions(*, stdout: str) -> list[str]:
    sessions: list[str] = []
    for raw_line in stdout.splitlines():
        line: str = raw_line.strip()
        if len(line) == 0:
            continue
        head, _sep, _rest = line.partition(":")
        name: str = head.strip()
        if len(name) > 0:
            sessions.append(name)
    return sessions


def _parse_gpu_utilization_max(*, stdout: str) -> int | None:
    values: list[int] = []
    for raw_line in stdout.splitlines():
        line: str = raw_line.strip()
        if len(line) == 0:
            continue
        try:
            values.append(int(line))
        except ValueError:
            continue
    if len(values) == 0:
        return None
    return max(values)


def _parse_gpu_memory_used(*, stdout: str) -> int | None:
    for raw_line in stdout.splitlines():
        line: str = raw_line.strip()
        if len(line) == 0:
            continue
        try:
            return int(line)
        except ValueError:
            continue
    return None


def _http_ok(*, result: HttpResult) -> bool:
    return HTTP_OK_LOWER <= result.status_code < HTTP_OK_UPPER


def probe(
    *,
    ssh_host: str,
    ssh_port: int,
    ssh_user: str,
    engine_url: str | None,
    log_path: str | None,
    ssh_runner: Callable[[list[str]], SshCommandResult] | None = None,
    http_runner: Callable[[str], HttpResult] | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> SshHealthReport:
    """Run a read-only diagnostic probe of a remote machine."""

    resolved_ssh: Callable[[list[str]], SshCommandResult] = (
        ssh_runner
        if ssh_runner is not None
        else _default_ssh_runner(
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            ssh_user=ssh_user,
            timeout_seconds=timeout_seconds,
        )
    )

    try:
        tmux_result: SshCommandResult = resolved_ssh(["sh", "-c", TMUX_LIST_CMD])
    except (ConnectionError, OSError, TimeoutError) as ssh_err:
        return SshHealthReport(
            ssh_reachable=False,
            tmux_sessions=[],
            gpu_utilization_percent=None,
            gpu_memory_used_mb=None,
            engine_health_ok=None,
            engine_completion_ok=None,
            recent_log_tail=None,
            error=str(ssh_err),
        )

    tmux_sessions: list[str] = _parse_tmux_sessions(stdout=tmux_result.stdout)

    util_result: SshCommandResult = resolved_ssh(["sh", "-c", NVIDIA_SMI_UTILIZATION_CMD])
    gpu_utilization: int | None = _parse_gpu_utilization_max(stdout=util_result.stdout)

    memory_result: SshCommandResult = resolved_ssh(["sh", "-c", NVIDIA_SMI_MEMORY_CMD])
    gpu_memory: int | None = _parse_gpu_memory_used(stdout=memory_result.stdout)

    recent_log_tail: str | None = None
    if log_path is not None:
        tail_cmd: str = f"tail -n {TAIL_LINES} {shlex.quote(log_path)}"
        tail_result: SshCommandResult = resolved_ssh(["sh", "-c", tail_cmd])
        recent_log_tail = tail_result.stdout

    engine_health_ok: bool | None = None
    engine_completion_ok: bool | None = None
    if engine_url is not None:
        resolved_http: Callable[[str], HttpResult] = (
            http_runner
            if http_runner is not None
            else _default_http_runner(timeout_seconds=timeout_seconds)
        )
        health_url: str = engine_url.rstrip("/") + HEALTH_ENDPOINT
        completions_url: str = engine_url.rstrip("/") + COMPLETIONS_ENDPOINT
        health_result: HttpResult = resolved_http(health_url)
        engine_health_ok = _http_ok(result=health_result)
        completion_result: HttpResult = resolved_http(completions_url)
        engine_completion_ok = _http_ok(result=completion_result)

    return SshHealthReport(
        ssh_reachable=True,
        tmux_sessions=tmux_sessions,
        gpu_utilization_percent=gpu_utilization,
        gpu_memory_used_mb=gpu_memory,
        engine_health_ok=engine_health_ok,
        engine_completion_ok=engine_completion_ok,
        recent_log_tail=recent_log_tail,
        error=None,
    )
