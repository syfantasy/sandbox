from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

import httpx

from app.config import Settings
from app.models import ExecRequest
from app.runner import SandboxRunner


def make_runner(
    tmp_path: Path,
    output_limit: int = 100_000,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> SandboxRunner:
    return SandboxRunner(
        Settings(
            session_root=tmp_path / "sessions",
            cache_root=tmp_path / "cache",
            token_sha256="0" * 64,
            max_timeout_seconds=5,
            default_timeout_seconds=2,
            max_output_bytes=output_limit,
            max_input_bytes=100_000,
            max_file_output_bytes=100_000,
            max_output_files=4,
            max_concurrent_jobs=1,
        ),
        http_transport=http_transport,
    )


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_exec_and_session_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = make_runner(Path(directory))
            first = await runner.execute(
                ExecRequest(command="printf hello > value.txt", session_id="demo")
            )
            second = await runner.execute(
                ExecRequest(command="cat value.txt", session_id="demo")
            )

            self.assertEqual(first.exit_code, 0)
            self.assertEqual(second.stdout, "hello")
            self.assertEqual(second.session_id, "demo")

    async def test_timeout_kills_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = make_runner(Path(directory))
            result = await runner.execute(
                ExecRequest(command="sleep 2", timeout_seconds=1)
            )

            self.assertEqual(result.status, "timed_out")
            self.assertTrue(result.timed_out)

    async def test_output_limit_stops_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = make_runner(Path(directory), output_limit=1024)
            result = await runner.execute(
                ExecRequest(command="python3 -c 'print(\"x\" * 10000)'")
            )

            self.assertEqual(result.status, "output_limit_exceeded")
            self.assertTrue(result.output_truncated)
            self.assertLessEqual(len(result.stdout.encode()), 1024)

    async def test_missing_argv_command_returns_structured_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = make_runner(Path(directory))
            result = await runner.execute(
                ExecRequest(command=["definitely-not-a-real-command"], shell=False)
            )

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.exit_code, 127)
            self.assertIn("No such file", result.stderr)

    async def test_input_and_output_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = make_runner(Path(directory))
            content = base64.b64encode(b"image-like-content").decode()
            result = await runner.execute(
                ExecRequest(
                    command="mkdir -p outputs && cp inputs/source.bin outputs/result.bin",
                    input_files=[
                        {"path": "inputs/source.bin", "content_base64": content}
                    ],
                    output_files=["outputs/*"],
                )
            )

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(len(result.files), 1)
            self.assertEqual(result.files[0].path, "outputs/result.bin")
            self.assertEqual(
                base64.b64decode(result.files[0].content_base64),
                b"image-like-content",
            )

    async def test_url_input_is_downloaded_inside_sandbox(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(str(request.url), "https://example.com/reference.png")
            return httpx.Response(200, content=b"downloaded-image")

        with tempfile.TemporaryDirectory() as directory:
            runner = make_runner(
                Path(directory),
                http_transport=httpx.MockTransport(handler),
            )
            result = await runner.execute(
                ExecRequest(
                    command="cat inputs/reference.img",
                    input_urls=[
                        {
                            "path": "inputs/reference.img",
                            "url": "https://example.com/reference.png",
                        }
                    ],
                )
            )

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.stdout, "downloaded-image")
