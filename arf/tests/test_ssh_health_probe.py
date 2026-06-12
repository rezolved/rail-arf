"""Tests for the SSH health probe (arf.scripts.utils.ssh_health_probe).

The probe builds an ``SshHealthReport`` from injectable ``ssh_runner`` and
``http_runner`` callables so tests can stub network access entirely. The
contract under test:

* When SSH and HTTP succeed, every report field is populated as parsed from
  the underlying responses (tmux list, nvidia-smi rows, /health, /v1/completions,
  log tail).
* When the engine ``/health`` endpoint returns non-200, ``engine_health_ok`` is
  ``False``; the completion probe is still attempted independently.
* When the SSH runner raises a connection error, ``ssh_reachable`` is ``False``
  and the rest of the report short-circuits to ``None``/empty values.
* ``engine_url=None`` skips both HTTP probes (both fields ``None``).
* ``log_path=None`` skips the log tail.
* ``gpu_utilization_percent`` is the MAX over GPUs parsed from the nvidia-smi
  csv,noheader,nounits output.

The implementation does not yet exist; these tests fail with an import error.
"""

from collections.abc import Callable
from dataclasses import dataclass

from arf.scripts.utils.ssh_health_probe import (
    HttpResult,
    SshCommandResult,
    SshHealthReport,
    probe,
)

SSH_HOST: str = "vm.example.test"
SSH_PORT: int = 22
SSH_USER: str = "ubuntu"
ENGINE_URL: str = "http://localhost:8000"
LOG_PATH: str = "/home/ubuntu/engine.log"

HEALTH_ENDPOINT_SUFFIX: str = "/health"
COMPLETIONS_ENDPOINT_SUFFIX: str = "/v1/completions"

NVIDIA_SMI_TWO_GPUS_STDOUT: str = "45\n23"
EXPECTED_GPU_UTIL_MAX: int = 45
GPU_MEMORY_USED_MB: int = 1234

TMUX_LIST_STDOUT: str = "engine: 1 windows\nmonitor: 1 windows"
EXPECTED_TMUX_SESSIONS: list[str] = ["engine", "monitor"]

LOG_TAIL_STDOUT: str = "line 1\nline 2\nline 3"
HEALTH_OK_BODY: str = '{"status":"ok"}'
COMPLETION_OK_BODY: str = '{"choices":[{"text":"x"}]}'

HTTP_OK: int = 200
HTTP_SERVICE_UNAVAILABLE: int = 503

NVIDIA_SMI_TOKEN: str = "nvidia-smi"
TMUX_TOKEN: str = "tmux"
TAIL_TOKEN: str = "tail"


@dataclass(frozen=True, slots=True)
class _RecordedSshCall:
    args: list[str]


@dataclass(frozen=True, slots=True)
class _RecordedHttpCall:
    url: str


class _SshRunnerStub:
    def __init__(
        self,
        *,
        tmux_stdout: str = TMUX_LIST_STDOUT,
        nvidia_smi_stdout: str = NVIDIA_SMI_TWO_GPUS_STDOUT,
        nvidia_smi_memory_stdout: str = str(GPU_MEMORY_USED_MB),
        log_tail_stdout: str = LOG_TAIL_STDOUT,
        raise_on_call: Exception | None = None,
    ) -> None:
        self._tmux_stdout: str = tmux_stdout
        self._nvidia_smi_stdout: str = nvidia_smi_stdout
        self._nvidia_smi_memory_stdout: str = nvidia_smi_memory_stdout
        self._log_tail_stdout: str = log_tail_stdout
        self._raise_on_call: Exception | None = raise_on_call
        self.calls: list[_RecordedSshCall] = []

    def __call__(self, args: list[str]) -> SshCommandResult:
        self.calls.append(_RecordedSshCall(args=list(args)))
        if self._raise_on_call is not None:
            raise self._raise_on_call
        joined: str = " ".join(args)
        if TAIL_TOKEN in joined:
            return SshCommandResult(
                returncode=0,
                stdout=self._log_tail_stdout,
                stderr="",
            )
        if NVIDIA_SMI_TOKEN in joined and "memory" in joined:
            return SshCommandResult(
                returncode=0,
                stdout=self._nvidia_smi_memory_stdout,
                stderr="",
            )
        if NVIDIA_SMI_TOKEN in joined:
            return SshCommandResult(
                returncode=0,
                stdout=self._nvidia_smi_stdout,
                stderr="",
            )
        if TMUX_TOKEN in joined:
            return SshCommandResult(
                returncode=0,
                stdout=self._tmux_stdout,
                stderr="",
            )
        return SshCommandResult(returncode=0, stdout="", stderr="")


class _HttpRunnerStub:
    def __init__(
        self,
        *,
        health_status: int = HTTP_OK,
        health_body: str = HEALTH_OK_BODY,
        completion_status: int = HTTP_OK,
        completion_body: str = COMPLETION_OK_BODY,
    ) -> None:
        self._health_status: int = health_status
        self._health_body: str = health_body
        self._completion_status: int = completion_status
        self._completion_body: str = completion_body
        self.calls: list[_RecordedHttpCall] = []

    def __call__(self, url: str) -> HttpResult:
        self.calls.append(_RecordedHttpCall(url=url))
        if url.endswith(HEALTH_ENDPOINT_SUFFIX):
            return HttpResult(
                status_code=self._health_status,
                body=self._health_body,
            )
        if url.endswith(COMPLETIONS_ENDPOINT_SUFFIX):
            return HttpResult(
                status_code=self._completion_status,
                body=self._completion_body,
            )
        return HttpResult(status_code=HTTP_OK, body="")


def _make_probe(
    *,
    ssh_runner: Callable[[list[str]], SshCommandResult],
    http_runner: Callable[[str], HttpResult] | None,
    engine_url: str | None = ENGINE_URL,
    log_path: str | None = LOG_PATH,
) -> SshHealthReport:
    return probe(
        ssh_host=SSH_HOST,
        ssh_port=SSH_PORT,
        ssh_user=SSH_USER,
        engine_url=engine_url,
        log_path=log_path,
        ssh_runner=ssh_runner,
        http_runner=http_runner,
    )


# ---------------------------------------------------------------------------
# Happy path: alive engine yields full report
# ---------------------------------------------------------------------------


def test_probe_alive_engine_full_report() -> None:
    ssh_runner: _SshRunnerStub = _SshRunnerStub()
    http_runner: _HttpRunnerStub = _HttpRunnerStub()

    report: SshHealthReport = _make_probe(
        ssh_runner=ssh_runner,
        http_runner=http_runner,
    )

    assert report.ssh_reachable is True
    assert report.tmux_sessions == EXPECTED_TMUX_SESSIONS
    assert report.gpu_utilization_percent == EXPECTED_GPU_UTIL_MAX
    assert report.gpu_memory_used_mb == GPU_MEMORY_USED_MB
    assert report.engine_health_ok is True
    assert report.engine_completion_ok is True
    assert report.recent_log_tail == LOG_TAIL_STDOUT
    assert report.error is None


# ---------------------------------------------------------------------------
# Dead engine: /health returns 503 → engine_health_ok=False
# ---------------------------------------------------------------------------


def test_probe_dead_engine_health_fails() -> None:
    ssh_runner: _SshRunnerStub = _SshRunnerStub()
    http_runner: _HttpRunnerStub = _HttpRunnerStub(
        health_status=HTTP_SERVICE_UNAVAILABLE,
        health_body="",
    )

    report: SshHealthReport = _make_probe(
        ssh_runner=ssh_runner,
        http_runner=http_runner,
    )

    assert report.ssh_reachable is True
    assert report.engine_health_ok is False
    # The completion probe is attempted independently of the /health result.
    completion_calls: list[_RecordedHttpCall] = [
        call for call in http_runner.calls if call.url.endswith(COMPLETIONS_ENDPOINT_SUFFIX)
    ]
    assert len(completion_calls) >= 1, "completion probe must still be attempted when /health fails"


# ---------------------------------------------------------------------------
# SSH unreachable: short-circuit the entire report
# ---------------------------------------------------------------------------


def test_probe_ssh_unreachable_short_circuits() -> None:
    ssh_runner: _SshRunnerStub = _SshRunnerStub(
        raise_on_call=ConnectionError("ssh: connect to host vm.example.test: timed out"),
    )
    http_runner: _HttpRunnerStub = _HttpRunnerStub()

    report: SshHealthReport = _make_probe(
        ssh_runner=ssh_runner,
        http_runner=http_runner,
    )

    assert report.ssh_reachable is False
    assert report.tmux_sessions == []
    assert report.gpu_utilization_percent is None
    assert report.gpu_memory_used_mb is None
    assert report.engine_health_ok is None
    assert report.engine_completion_ok is None
    assert report.recent_log_tail is None
    assert report.error is not None
    assert len(report.error) > 0


# ---------------------------------------------------------------------------
# No engine_url: skip both HTTP probes
# ---------------------------------------------------------------------------


def test_probe_no_engine_url_skips_http_probes() -> None:
    ssh_runner: _SshRunnerStub = _SshRunnerStub()
    http_runner: _HttpRunnerStub = _HttpRunnerStub()

    report: SshHealthReport = _make_probe(
        ssh_runner=ssh_runner,
        http_runner=http_runner,
        engine_url=None,
    )

    assert report.engine_health_ok is None
    assert report.engine_completion_ok is None
    assert len(http_runner.calls) == 0, "http_runner must not be called when engine_url is None"


# ---------------------------------------------------------------------------
# No log_path: skip the log tail
# ---------------------------------------------------------------------------


def test_probe_no_log_path_skips_tail() -> None:
    ssh_runner: _SshRunnerStub = _SshRunnerStub()
    http_runner: _HttpRunnerStub = _HttpRunnerStub()

    report: SshHealthReport = _make_probe(
        ssh_runner=ssh_runner,
        http_runner=http_runner,
        log_path=None,
    )

    assert report.recent_log_tail is None
    tail_calls: list[_RecordedSshCall] = [
        call for call in ssh_runner.calls if any(TAIL_TOKEN in arg for arg in call.args)
    ]
    assert len(tail_calls) == 0, "tail must not be executed when log_path is None"


# ---------------------------------------------------------------------------
# nvidia-smi: parse MAX utilization across multiple GPUs
# ---------------------------------------------------------------------------


def test_probe_parses_nvidia_smi_max_across_gpus() -> None:
    ssh_runner: _SshRunnerStub = _SshRunnerStub(
        nvidia_smi_stdout=NVIDIA_SMI_TWO_GPUS_STDOUT,
    )
    http_runner: _HttpRunnerStub = _HttpRunnerStub()

    report: SshHealthReport = _make_probe(
        ssh_runner=ssh_runner,
        http_runner=http_runner,
    )

    assert report.gpu_utilization_percent == EXPECTED_GPU_UTIL_MAX
