from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from logscribe.sampler import RawErrorChunk

TIMESTAMP_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)")
ERROR_TYPE_PATTERN = re.compile(r"\b([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception))\b")
KEY_VALUE_PATTERN = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*([^\s,;]+)")


@dataclass
class ErrorEvent:
    """A structured representation of a captured error, ready for embedding and storage."""

    id: str
    error_type: str
    message: str
    stack_trace: str
    key_variables: dict[str, str]
    timestamp: str
    source_file: str
    line_number: int
    raw_context: str

    def to_document(self) -> str:
        """Flatten the event into a single text blob suitable for embedding."""
        parts = [f"error_type: {self.error_type}", f"message: {self.message}"]
        if self.key_variables:
            kv = ", ".join(f"{k}={v}" for k, v in self.key_variables.items())
            parts.append(f"variables: {kv}")
        if self.stack_trace:
            parts.append(f"stack_trace:\n{self.stack_trace}")
        return "\n".join(parts)


class ErrorProcessor:
    """Turns a RawErrorChunk (raw log lines) into a structured ErrorEvent."""

    def process(self, chunk: RawErrorChunk) -> ErrorEvent:
        context_text = "\n".join(chunk.context_lines)

        error_type = self._extract_error_type(chunk.trigger_line, context_text)
        message = self._extract_message(chunk.trigger_line, error_type)
        stack_trace = self._extract_stack_trace(chunk.context_lines)
        key_variables = self._extract_key_variables(context_text)
        timestamp = self._extract_timestamp(chunk.trigger_line, context_text)

        return ErrorEvent(
            id=str(uuid.uuid4()),
            error_type=error_type,
            message=message,
            stack_trace=stack_trace,
            key_variables=key_variables,
            timestamp=timestamp,
            source_file=chunk.source_file,
            line_number=chunk.line_number,
            raw_context=context_text,
        )

    @staticmethod
    def _extract_error_type(trigger_line: str, context_text: str) -> str:
        match = ERROR_TYPE_PATTERN.search(trigger_line) or ERROR_TYPE_PATTERN.search(context_text)
        if match:
            return match.group(1)
        for keyword in ("CRITICAL", "FATAL", "ERROR"):
            if keyword in trigger_line:
                return keyword
        return "UnknownError"

    @staticmethod
    def _extract_message(trigger_line: str, error_type: str) -> str:
        if error_type in trigger_line:
            idx = trigger_line.find(error_type)
            tail = trigger_line[idx + len(error_type):].lstrip(" :-")
            if tail:
                return tail
        return trigger_line.strip()

    @staticmethod
    def _extract_stack_trace(context_lines: list[str]) -> str:
        for i, line in enumerate(context_lines):
            if "Traceback (most recent call last)" in line:
                return "\n".join(context_lines[i:])
        return ""

    @staticmethod
    def _extract_key_variables(context_text: str) -> dict[str, str]:
        return dict(KEY_VALUE_PATTERN.findall(context_text))

    @staticmethod
    def _extract_timestamp(trigger_line: str, context_text: str) -> str:
        match = TIMESTAMP_PATTERN.search(trigger_line) or TIMESTAMP_PATTERN.search(context_text)
        if match:
            return match.group(1)
        return datetime.now(timezone.utc).isoformat()
