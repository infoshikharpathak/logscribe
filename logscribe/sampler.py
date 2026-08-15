from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Pattern

DEFAULT_ERROR_PATTERNS: list[str] = [
    r"\bERROR\b",
    r"\bCRITICAL\b",
    r"\bFATAL\b",
    r"\bException\b",
    r"Traceback \(most recent call last\)",
]


@dataclass
class RawErrorChunk:
    """A window of raw log lines captured around a detected error trigger."""

    trigger_line: str
    context_lines: list[str]
    source_file: str
    line_number: int


class RollingBuffer:
    """Fixed-size FIFO buffer of the most recently seen log lines."""

    def __init__(self, maxlen: int = 50) -> None:
        self._lines: deque[str] = deque(maxlen=maxlen)

    def append(self, line: str) -> None:
        self._lines.append(line)

    def snapshot(self) -> list[str]:
        return list(self._lines)


class LogSampler:
    """Tails a log file and yields a RawErrorChunk each time a configured error pattern matches."""

    def __init__(
        self,
        file_path: str | Path,
        patterns: list[str] | None = None,
        buffer_size: int = 50,
        poll_interval: float = 0.5,
    ) -> None:
        self.file_path = Path(file_path)
        self._patterns: list[Pattern[str]] = [
            re.compile(p) for p in (patterns or DEFAULT_ERROR_PATTERNS)
        ]
        self._buffer = RollingBuffer(maxlen=buffer_size)
        self.poll_interval = poll_interval

    def _is_error_line(self, line: str) -> bool:
        return any(pattern.search(line) for pattern in self._patterns)

    def tail(self, from_start: bool = False) -> Iterator[RawErrorChunk]:
        """Continuously tail the file, yielding a RawErrorChunk whenever an error line appears.

        Uses simple polling rather than a filesystem-events dependency (e.g. watchdog)
        so the sampler has zero extra runtime dependencies; swap in watchdog later if
        polling latency ever becomes a problem.
        """
        with self.file_path.open("r", errors="replace") as f:
            if not from_start:
                f.seek(0, 2)  # jump to end of file

            line_number = 0
            while True:
                line = f.readline()
                if not line:
                    time.sleep(self.poll_interval)
                    continue

                line_number += 1
                stripped = line.rstrip("\n")
                self._buffer.append(stripped)

                if not self._is_error_line(stripped):
                    continue

                line_number = self._absorb_traceback(f, line_number)

                yield RawErrorChunk(
                    trigger_line=stripped,
                    context_lines=self._buffer.snapshot(),
                    source_file=str(self.file_path),
                    line_number=line_number,
                )

    def _absorb_traceback(self, f, line_number: int) -> int:
        """Pull a Python-style traceback into the buffer if one immediately follows
        the trigger line, so stack_trace extraction has the full block to work with.

        Log writers typically emit the summary line ("ERROR ...") followed
        immediately by "Traceback (most recent call last):" and its indented
        frames; without this lookahead the trigger fires before those lines
        exist and the traceback is lost.
        """
        pos = f.tell()
        line = f.readline()
        if not line or "Traceback (most recent call last)" not in line:
            if line:
                f.seek(pos)  # not a traceback continuation — put it back
            return line_number

        line_number += 1
        self._buffer.append(line.rstrip("\n"))

        while True:
            line = f.readline()
            if not line:
                break
            stripped = line.rstrip("\n")
            line_number += 1
            self._buffer.append(stripped)
            if not stripped.startswith((" ", "\t")):
                break  # final "SomeError: message" line — traceback is complete

        return line_number


def scan_lines(
    lines: list[str],
    patterns: list[str] | None = None,
    buffer_size: int = 50,
    source_file: str = "<pasted text>",
) -> list[RawErrorChunk]:
    """Run the same trigger-detection + rolling-buffer + traceback-absorption logic as
    LogSampler.tail(), but over a static in-memory list instead of a live file handle.

    Used by the UI's paste-a-snippet flow, where there's no file to tail.
    """
    compiled = [re.compile(p) for p in (patterns or DEFAULT_ERROR_PATTERNS)]
    buffer = RollingBuffer(maxlen=buffer_size)
    chunks: list[RawErrorChunk] = []

    i = 0
    while i < len(lines):
        stripped = lines[i].rstrip("\n")
        buffer.append(stripped)
        i += 1

        if not any(pattern.search(stripped) for pattern in compiled):
            continue

        if i < len(lines) and "Traceback (most recent call last)" in lines[i]:
            buffer.append(lines[i].rstrip("\n"))
            i += 1
            while i < len(lines):
                tb_line = lines[i].rstrip("\n")
                buffer.append(tb_line)
                i += 1
                if not tb_line.startswith((" ", "\t")):
                    break

        chunks.append(
            RawErrorChunk(
                trigger_line=stripped,
                context_lines=buffer.snapshot(),
                source_file=source_file,
                line_number=i,
            )
        )

    return chunks
