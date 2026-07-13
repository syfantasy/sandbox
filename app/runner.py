from __future__ import annotations

import asyncio
import base64
import binascii
import mimetypes
import os
import shlex
import shutil
import signal
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Settings
from .models import ExecRequest, ExecResponse, OutputFile, StepResult


@dataclass(slots=True)
class _ProcessResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    output_truncated: bool
    duration_seconds: float


class _OutputState:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.total = 0
        self.truncated = False

    def take(self, chunk: bytes) -> bytes:
        available = max(self.limit - self.total, 0)
        kept = chunk[:available]
        self.total += len(kept)
        if len(kept) != len(chunk):
            self.truncated = True
        return kept


class SandboxRunner:
    def __init__(
        self,
        settings: Settings,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._http_transport = http_transport
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)
        self._active_jobs = 0
        self.settings.session_root.mkdir(parents=True, exist_ok=True)
        self.settings.cache_root.mkdir(parents=True, exist_ok=True)

    @property
    def active_jobs(self) -> int:
        return self._active_jobs

    def session_path(self, session_id: str) -> Path:
        return self.settings.session_root / session_id

    async def execute(self, request: ExecRequest) -> ExecResponse:
        async with self._semaphore:
            self._active_jobs += 1
            try:
                return await self._execute_locked(request)
            finally:
                self._active_jobs -= 1

    async def _execute_locked(self, request: ExecRequest) -> ExecResponse:
        started = time.monotonic()
        job_id = uuid.uuid4().hex
        session_id = request.session_id or uuid.uuid4().hex
        timeout = min(
            request.timeout_seconds or self.settings.default_timeout_seconds,
            self.settings.max_timeout_seconds,
        )
        deadline = started + timeout
        session_dir = self.session_path(session_id)
        session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        home_dir = session_dir / ".home"
        home_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

        reset_error = self._reset_paths(session_dir, request)
        if reset_error:
            return ExecResponse(
                job_id=job_id,
                session_id=session_id,
                cwd=str(session_dir),
                status="setup_failed",
                exit_code=2,
                stdout="",
                stderr=reset_error,
                timed_out=False,
                output_truncated=False,
                duration_seconds=round(time.monotonic() - started, 4),
            )

        input_error = self._write_input_files(session_dir, request)
        if input_error:
            return ExecResponse(
                job_id=job_id,
                session_id=session_id,
                cwd=str(session_dir),
                status="setup_failed",
                exit_code=2,
                stdout="",
                stderr=input_error,
                timed_out=False,
                output_truncated=False,
                duration_seconds=round(time.monotonic() - started, 4),
            )

        download_error = await self._download_input_urls(
            session_dir, request, deadline
        )
        if download_error:
            return ExecResponse(
                job_id=job_id,
                session_id=session_id,
                cwd=str(session_dir),
                status="setup_failed",
                exit_code=2,
                stdout="",
                stderr=download_error,
                timed_out=False,
                output_truncated=False,
                duration_seconds=round(time.monotonic() - started, 4),
            )

        cwd = self._resolve_cwd(session_dir, request.cwd)
        cwd.mkdir(parents=True, exist_ok=True)
        environment = self._build_environment(session_dir, home_dir, request.env)

        setup_steps: list[StepResult] = []

        if request.python_packages:
            python_steps = []
            venv_python = session_dir / ".venv" / "bin" / "python"
            if not venv_python.exists():
                python_steps.append(
                    (
                        "create_python_environment",
                        [
                            "uv",
                            "venv",
                            "--system-site-packages",
                            str(session_dir / ".venv"),
                        ],
                    )
                )
            python_steps.append(
                (
                    "install_python_packages",
                    [
                        "uv",
                        "pip",
                        "install",
                        "--python",
                        str(venv_python),
                        "--",
                        *request.python_packages,
                    ],
                )
            )
            failure = await self._run_setup_steps(
                python_steps, cwd, environment, deadline, setup_steps
            )
            if failure is not None:
                return self._setup_failure_response(
                    job_id,
                    session_id,
                    cwd,
                    started,
                    setup_steps,
                    failure,
                )

        if request.node_packages:
            node_modules = session_dir / "node_modules"
            failure = await self._run_setup_steps(
                [
                    (
                        "install_node_packages",
                        [
                            "npm",
                            "install",
                            "--prefix",
                            str(session_dir),
                            "--no-audit",
                            "--no-fund",
                            "--",
                            *request.node_packages,
                        ],
                    )
                ],
                cwd,
                environment,
                deadline,
                setup_steps,
            )
            if failure is not None:
                return self._setup_failure_response(
                    job_id,
                    session_id,
                    cwd,
                    started,
                    setup_steps,
                    failure,
                )
            environment["NODE_PATH"] = str(node_modules)

        remaining = max(deadline - time.monotonic(), 0.001)
        command = self._command_argv(request)
        result = await self._run_process(
            command,
            cwd=cwd,
            environment=environment,
            timeout=remaining,
            stdin=request.stdin,
        )
        status = "completed"
        if result.timed_out:
            status = "timed_out"
        elif result.output_truncated:
            status = "output_limit_exceeded"

        files, files_truncated = self._collect_output_files(session_dir, request)

        return ExecResponse(
            job_id=job_id,
            session_id=session_id,
            cwd=str(cwd),
            status=status,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=result.timed_out,
            output_truncated=result.output_truncated,
            duration_seconds=round(time.monotonic() - started, 4),
            setup_steps=setup_steps,
            files=files,
            files_truncated=files_truncated,
        )

    async def _run_setup_steps(
        self,
        steps: list[tuple[str, list[str]]],
        cwd: Path,
        environment: dict[str, str],
        deadline: float,
        results: list[StepResult],
    ) -> _ProcessResult | None:
        for name, command in steps:
            remaining = max(deadline - time.monotonic(), 0.001)
            result = await self._run_process(
                command,
                cwd=cwd,
                environment=environment,
                timeout=remaining,
                stdin=None,
            )
            results.append(
                StepResult(
                    name=name,
                    exit_code=result.exit_code,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    timed_out=result.timed_out,
                    output_truncated=result.output_truncated,
                    duration_seconds=round(result.duration_seconds, 4),
                )
            )
            if result.exit_code != 0 or result.timed_out or result.output_truncated:
                return result
        return None

    def _setup_failure_response(
        self,
        job_id: str,
        session_id: str,
        cwd: Path,
        started: float,
        setup_steps: list[StepResult],
        failure: _ProcessResult,
    ) -> ExecResponse:
        return ExecResponse(
            job_id=job_id,
            session_id=session_id,
            cwd=str(cwd),
            status="setup_failed",
            exit_code=failure.exit_code,
            stdout="",
            stderr="Dependency setup failed; inspect setup_steps for details.",
            timed_out=failure.timed_out,
            output_truncated=failure.output_truncated,
            duration_seconds=round(time.monotonic() - started, 4),
            setup_steps=setup_steps,
        )

    def _resolve_cwd(self, session_dir: Path, requested: str | None) -> Path:
        if not requested:
            return session_dir
        path = Path(requested).expanduser()
        if not path.is_absolute():
            path = session_dir / path
        return path.resolve()

    def _write_input_files(self, session_dir: Path, request: ExecRequest) -> str | None:
        total_bytes = 0
        session_root = session_dir.resolve()
        for uploaded in request.input_files:
            try:
                content = base64.b64decode(uploaded.content_base64, validate=True)
            except (ValueError, binascii.Error):
                return f"Invalid base64 content for input file: {uploaded.path}"
            total_bytes += len(content)
            if total_bytes > self.settings.max_input_bytes:
                return "Input files exceed MAX_INPUT_BYTES"

            destination = (session_dir / uploaded.path).resolve()
            if not destination.is_relative_to(session_root):
                return f"Input file escapes the session directory: {uploaded.path}"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        return None

    def _reset_paths(self, session_dir: Path, request: ExecRequest) -> str | None:
        session_root = session_dir.resolve()
        for relative in request.reset_paths:
            target = (session_dir / relative).resolve()
            if target == session_root or not target.is_relative_to(session_root):
                return f"Reset path escapes the session directory: {relative}"
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                target.unlink()
        return None

    async def _download_input_urls(
        self, session_dir: Path, request: ExecRequest, deadline: float
    ) -> str | None:
        if not request.input_urls:
            return None

        session_root = session_dir.resolve()
        total_bytes = 0
        timeout = httpx.Timeout(30.0, connect=15.0)
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": "PrivateRobotSandbox/0.2"},
            transport=self._http_transport,
        ) as client:
            for remote in request.input_urls:
                destination = (session_dir / remote.path).resolve()
                if not destination.is_relative_to(session_root):
                    return f"Input URL path escapes the session directory: {remote.path}"
                destination.parent.mkdir(parents=True, exist_ok=True)

                try:
                    remaining = max(deadline - time.monotonic(), 0.001)
                    async with asyncio.timeout(remaining):
                        async with client.stream("GET", remote.url) as response:
                            response.raise_for_status()
                            with destination.open("wb") as output:
                                async for chunk in response.aiter_bytes(65_536):
                                    total_bytes += len(chunk)
                                    if total_bytes > self.settings.max_input_bytes:
                                        output.close()
                                        destination.unlink(missing_ok=True)
                                        return "Downloaded input files exceed MAX_INPUT_BYTES"
                                    output.write(chunk)
                except TimeoutError:
                    destination.unlink(missing_ok=True)
                    return f"Timed out downloading {remote.url}"
                except httpx.HTTPError as exc:
                    destination.unlink(missing_ok=True)
                    return f"Failed to download {remote.url}: {exc}"
        return None

    def _collect_output_files(
        self, session_dir: Path, request: ExecRequest
    ) -> tuple[list[OutputFile], bool]:
        if not request.output_files:
            return [], False

        session_root = session_dir.resolve()
        candidates: dict[str, Path] = {}
        for pattern in request.output_files:
            for path in session_dir.glob(pattern):
                resolved = path.resolve()
                if (
                    resolved.is_relative_to(session_root)
                    and resolved.is_file()
                    and not resolved.is_symlink()
                ):
                    relative = str(resolved.relative_to(session_root))
                    candidates[relative] = resolved

        results: list[OutputFile] = []
        total_bytes = 0
        truncated = False
        for relative, path in sorted(candidates.items()):
            if len(results) >= self.settings.max_output_files:
                truncated = True
                break
            size = path.stat().st_size
            if total_bytes + size > self.settings.max_file_output_bytes:
                truncated = True
                continue
            content = path.read_bytes()
            total_bytes += len(content)
            results.append(
                OutputFile(
                    path=relative,
                    mime_type=self._guess_mime_type(path, content),
                    size=len(content),
                    content_base64=base64.b64encode(content).decode("ascii"),
                )
            )
        return results, truncated

    def _guess_mime_type(self, path: Path, content: bytes) -> str:
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if content.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
            return "image/webp"
        guessed, _ = mimetypes.guess_type(path.name)
        return guessed or "application/octet-stream"

    def _build_environment(
        self, session_dir: Path, home_dir: Path, requested: dict[str, str]
    ) -> dict[str, str]:
        venv_bin = session_dir / ".venv" / "bin"
        base_path = os.getenv(
            "PATH",
            "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin",
        )
        environment = {
            "PATH": f"{venv_bin}:{base_path}",
            "HOME": str(home_dir),
            "LANG": os.getenv("LANG", "C.UTF-8"),
            "LC_ALL": os.getenv("LC_ALL", "C.UTF-8"),
            "TERM": os.getenv("TERM", "xterm-256color"),
            "PYTHONUNBUFFERED": "1",
            "UV_CACHE_DIR": str(self.settings.cache_root / "uv"),
            "PIP_CACHE_DIR": str(self.settings.cache_root / "pip"),
            "npm_config_cache": str(self.settings.cache_root / "npm"),
            "SANDBOX_SESSION_ID": session_dir.name,
        }
        environment.update(requested)
        return environment

    def _command_argv(self, request: ExecRequest) -> list[str]:
        if isinstance(request.command, list):
            return request.command
        if request.shell:
            return ["/bin/bash", "-c", request.command]
        return shlex.split(request.command)

    async def _run_process(
        self,
        command: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout: float,
        stdin: str | None,
    ) -> _ProcessResult:
        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            exit_code = 127 if isinstance(exc, FileNotFoundError) else 126
            return _ProcessResult(
                exit_code=exit_code,
                stdout="",
                stderr=str(exc),
                timed_out=False,
                output_truncated=False,
                duration_seconds=time.monotonic() - started,
            )
        assert process.stdout is not None
        assert process.stderr is not None
        assert process.stdin is not None

        if stdin:
            process.stdin.write(stdin.encode("utf-8"))
        process.stdin.close()

        state = _OutputState(self.settings.max_output_bytes)
        limit_event = asyncio.Event()
        stdout_buffer = bytearray()
        stderr_buffer = bytearray()

        async def read_stream(
            stream: asyncio.StreamReader, buffer: bytearray
        ) -> None:
            while True:
                chunk = await stream.read(65_536)
                if not chunk:
                    return
                kept = state.take(chunk)
                buffer.extend(kept)
                if state.truncated:
                    limit_event.set()

        stdout_task = asyncio.create_task(read_stream(process.stdout, stdout_buffer))
        stderr_task = asyncio.create_task(read_stream(process.stderr, stderr_buffer))
        wait_task = asyncio.create_task(process.wait())
        limit_task = asyncio.create_task(limit_event.wait())

        timed_out = False
        try:
            done, _ = await asyncio.wait(
                {wait_task, limit_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if wait_task not in done:
                timed_out = limit_task not in done
                await self._terminate_process_group(process)
            await wait_task
        finally:
            limit_task.cancel()
            await asyncio.gather(limit_task, return_exceptions=True)
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

        return _ProcessResult(
            exit_code=process.returncode if process.returncode is not None else -1,
            stdout=stdout_buffer.decode("utf-8", errors="replace"),
            stderr=stderr_buffer.decode("utf-8", errors="replace"),
            timed_out=timed_out,
            output_truncated=state.truncated,
            duration_seconds=time.monotonic() - started,
        )

    async def _terminate_process_group(
        self, process: asyncio.subprocess.Process
    ) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=1.5)
            return
        except asyncio.TimeoutError:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        await process.wait()

    def cleanup_session(self, session_id: str) -> bool:
        path = self.session_path(session_id)
        if not path.exists():
            return False
        shutil.rmtree(path)
        return True
