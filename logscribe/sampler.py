
from __future__ import annotations

import json
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

# A structured JSON log line says its own level, so the level decides, not a
# regex over the serialized object (an INFO line whose message happens to
# say "Exception" isn't an error; an ERROR line always is).
ERROR_LEVELS = frozenset({"ERROR", "CRITICAL", "FATAL"})


def json_level(line: str) -> str | None:
    """The "level" of a JSON object log line, or None if it isn't one."""
    if not line.startswith("{"):
        return None
    try:
        parsed = json.loads(line)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("level"), str):
        return None
    return parsed["level"].upper()


def is_error_line(line: str, patterns: list[Pattern[str]]) -> bool:
    level = json_level(line)
    if level is not None:
        return level in ERROR_LEVELS
    return any(pattern.search(line) for pattern in patterns)


@dataclass
class RawErrorChunk:
    """A window of raw log lines captured around a detected error trigger."""

    trigger_line: str
    context_lines: list[str]
    source_file: str
    line_number: int
    service_name: str | None = None


class RollingBuffer:
    """Fixed-size FIFO buffer of the most recently seen log lines."""

    def __init__(self, maxlen: int = 50) -> None:
        self._lines: deque[str] = deque(maxlen=maxlen)

    def append(self, line: str) -> None:
        self._lines.append(line)

    def snapshot(self) -> list[str]:
        return list(self._lines)


class LogSampler:
    """Tails a log file and yields a RawErrorChunk each time a configured error pattern matches.

    *service_name* labels every chunk from this file (for structured logs,
    the line's own service_name field wins). The file may not exist yet: the
    sampler waits for it. If it shrinks (truncated or rotated), it's reopened
    from the start.
    """

    def __init__(
        self,
        file_path: str | Path,
        patterns: list[str] | None = None,
        buffer_size: int = 50,
        poll_interval: float = 0.5,
        service_name: str | None = None,
    ) -> None:
        self.file_path = Path(file_path)
        self._patterns: list[Pattern[str]] = [
            re.compile(p) for p in (patterns or DEFAULT_ERROR_PATTERNS)
        ]
        self._buffer = RollingBuffer(maxlen=buffer_size)
        self.poll_interval = poll_interval
        self.service_name = service_name
        self._f = None
        self._line_number = 0
        self._from_start = False

    def _is_error_line(self, line: str) -> bool:
        return is_error_line(line, self._patterns)

    def open(self, from_start: bool = False) -> None:
        self._from_start = from_start
        self._reopen(at_end=not from_start)

    def _reopen(self, at_end: bool) -> None:
        if self._f is not None:
            self._f.close()
            self._f = None
        self._line_number = 0
        if not self.file_path.exists():
            return  # created later: read it from its first line then
        self._f = self.file_path.open("r", errors="replace")
        if at_end:
            self._f.seek(0, 2)

    def close(self) -> None:
        if self._f is not None:
            self._f.close()
            self._f = None

    def read_available(self) -> list[RawErrorChunk]:
        """Read every complete line written since the last call; return the
        error chunks they contain (possibly none). Never blocks."""
        if self._f is None:
            if not self.file_path.exists():
                return []
            self._reopen(at_end=False)
        try:
            if self.file_path.stat().st_size < self._f.tell():
                self._reopen(at_end=False)  # truncated or rotated
        except FileNotFoundError:
            self.close()
            return []

        chunks: list[RawErrorChunk] = []
        while True:
            pos = self._f.tell()
            line = self._f.readline()
            if not line:
                break
            if not line.endswith("\n"):
                self._f.seek(pos)  # a line still being written: read it whole next time
                break
            self._line_number += 1
            stripped = line.rstrip("\n")
            self._buffer.append(stripped)
            if not self._is_error_line(stripped):
                continue
            self._line_number = self._absorb_traceback(self._f, self._line_number)
            chunks.append(RawErrorChunk(
                trigger_line=stripped,
                context_lines=self._buffer.snapshot(),
                source_file=str(self.file_path),
                line_number=self._line_number,
                service_name=self.service_name,
            ))
        return chunks

    def tail(self, from_start: bool = False) -> Iterator[RawErrorChunk]:
        """Continuously tail the file, yielding a RawErrorChunk whenever an error line appears.

        Uses simple polling rather than a filesystem-events dependency (e.g. watchdog)
        so the sampler has zero extra runtime dependencies; swap in watchdog later if
        polling latency ever becomes a problem.
        """
        self.open(from_start=from_start)
        try:
            while True:
                chunks = self.read_available()
                if not chunks:
                    time.sleep(self.poll_interval)
                yield from chunks
        finally:
            self.close()

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


class MultiFileSampler:
    """Tails several log files in one process. Each file keeps its own
    rolling buffer (context never mixes services) and service name."""

    def __init__(self, samplers: list[LogSampler], poll_interval: float = 0.5) -> None:
        if not samplers:
            raise ValueError("MultiFileSampler needs at least one file")
        self.samplers = samplers
        self.poll_interval = poll_interval

    def open(self, from_start: bool = False) -> None:
        for sampler in self.samplers:
            sampler.open(from_start=from_start)

    def read_available(self) -> list[RawErrorChunk]:
        return [chunk for sampler in self.samplers for chunk in sampler.read_available()]

    def tail(self, from_start: bool = False) -> Iterator[RawErrorChunk]:
        self.open(from_start=from_start)
        try:
            while True:
                chunks = self.read_available()
                if not chunks:
                    time.sleep(self.poll_interval)
                yield from chunks
        finally:
            for sampler in self.samplers:
                sampler.close()


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

        if not is_error_line(stripped, compiled):
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
