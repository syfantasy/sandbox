from __future__ import annotations

import base64
import json
import unittest

from app.main import iter_exec_stream
from app.models import ExecResponse, OutputFile


class StreamResponseTests(unittest.TestCase):
    def test_stream_metadata_and_chunks_reconstruct_file(self) -> None:
        content = b"media-content" * 40_000
        response = ExecResponse(
            job_id="job",
            session_id="session",
            cwd="/tmp/session",
            status="completed",
            exit_code=0,
            stdout="done",
            stderr="",
            timed_out=False,
            output_truncated=False,
            duration_seconds=0.1,
            files=[
                OutputFile(
                    path="outputs/result.mp4",
                    mime_type="video/mp4",
                    size=len(content),
                    content_base64=base64.b64encode(content).decode("ascii"),
                )
            ],
        )

        events = [json.loads(line) for line in iter_exec_stream(response)]

        self.assertEqual(events[0]["type"], "result")
        self.assertNotIn("content_base64", events[0]["data"]["files"][0])
        chunks = [
            event["content_base64"]
            for event in events
            if event["type"] == "file_chunk"
        ]
        self.assertGreater(len(chunks), 1)
        self.assertEqual(base64.b64decode("".join(chunks)), content)
        self.assertEqual(events[-1], {"type": "end"})
