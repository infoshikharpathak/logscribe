from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from logscribe.sampler import RawErrorChunk

TIMESTAMP_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)")
ERROR_TYPE_PATTERN = re.compile(r"\b([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception))\b")
KEY_VALUE_PATTERN = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*([^\s,;]+)")

# Fields already surfaced as dedicated ErrorEvent attributes when the log line is
# JSON — everything else in the object becomes a key_variable automatically.
_JSON_CORE_FIELDS = {"timestamp", "service_name", "level", "message", "error_type", "stack_trace"}

# Per-occurrence values (unique per request) are kept as key_variables but
# left out of the embedded document: they'd make two occurrences of the same
# error look less alike.
VOLATILE_FIELDS = {"request_id", "duration_ms", "trace_id", "attempts"}

# Fallback when a line has no request_id: this many of the service's latest lines.
RELATED_FALLBACK_LINES = 5


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
    service_name: str | None = None
    # The log lines about this failure: the same request's lines (by
    # request_id), else the same service's most recent lines. raw_context is
    # the whole rolling buffer, which can hold other requests' errors.
    related_lines: list[str] = field(default_factory=list)

    def related_context(self) -> str:
        return "\n".join(self.related_lines)

    def to_document(self) -> str:
        """Flatten the event into a single text blob suitable for embedding."""
        parts = [f"error_type: {self.error_type}", f"message: {self.message}"]
        if self.service_name:
            parts.insert(0, f"service: {self.service_name}")
        stable = {k: v for k, v in self.key_variables.items() if k not in VOLATILE_FIELDS}
        if stable:
            kv = ", ".join(f"{k}={v}" for k, v in stable.items())
            parts.append(f"variables: {kv}")
        if self.stack_trace:
            parts.append(f"stack_trace:\n{self.stack_trace}")
        return "\n".join(parts)

    def embedding_text(self) -> str:
        """What gets embedded and matched: the error's identity only (type,
        message, service, endpoint, the exception's last line). Variables and
        full stack traces share boilerplate across unrelated errors (method,
        status code, framework frames, paths) that would outweigh it; they
        stay in to_document(), which the analysis prompt uses."""
        parts = [f"error_type: {self.error_type}", f"message: {self.message}"]
        if self.service_name:
            parts.append(f"service: {self.service_name}")
        if self.key_variables.get("endpoint"):
            parts.append(f"endpoint: {self.key_variables['endpoint']}")
        last = next((ln.strip() for ln in reversed(self.stack_trace.splitlines()) if ln.strip()), "")
        if last and last not in self.message:
            parts.append(f"exception: {last}")
        return "\n".join(parts)


class ErrorProcessor:
    """Turns a RawErrorChunk (raw log lines) into a structured ErrorEvent."""

    def process(self, chunk: RawErrorChunk) -> ErrorEvent:
        context_text = "\n".join(chunk.context_lines)

        json_fields = self._try_parse_json(chunk.trigger_line)
        if json_fields is not None:
            return self._process_json(chunk, json_fields, context_text)
        return self._process_text(chunk, context_text)

    def _process_text(self, chunk: RawErrorChunk, context_text: str) -> ErrorEvent:
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
            service_name=chunk.service_name,
            related_lines=chunk.context_lines[-RELATED_FALLBACK_LINES:],
        )

    def _process_json(self, chunk: RawErrorChunk, fields: dict, context_text: str) -> ErrorEvent:
        """Structured JSON logs (e.g. fooddash) carry their own message/error_type/
        timestamp directly — read them instead of running text regexes over the
        object's serialized form, which finds fields by accident (if at all) and
        mangles anything positional, like `message` extraction."""
        error_type = str(fields.get("error_type") or fields.get("level") or "UnknownError")
        message = str(fields.get("message", chunk.trigger_line))
        # Only this line's own trace (or a plain-text traceback written after
        # it): searching the whole buffer would take another request's
        # stack_trace field as this error's.
        trigger_at = max(i for i, line in enumerate(chunk.context_lines) if line == chunk.trigger_line) \
            if chunk.trigger_line in chunk.context_lines else len(chunk.context_lines)
        stack_trace = str(fields["stack_trace"]) if fields.get("stack_trace") else \
            self._extract_stack_trace(chunk.context_lines[trigger_at + 1:])
        timestamp = str(fields["timestamp"]) if fields.get("timestamp") else self._extract_timestamp(chunk.trigger_line, context_text)
        key_variables = {
            key: str(value) for key, value in fields.items() if key not in _JSON_CORE_FIELDS and value is not None
        }

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
            service_name=str(fields["service_name"]) if fields.get("service_name") else chunk.service_name,
            related_lines=self._related_json_lines(chunk, fields, trigger_at),
        )

    def _related_json_lines(self, chunk: RawErrorChunk, fields: dict, trigger_at: int) -> list[str]:
        """This request's lines up to and including the error (by request_id);
        without a request_id, the same service's last few lines."""
        lines = chunk.context_lines[: trigger_at + 1]
        parsed = [(line, self._try_parse_json(line)) for line in lines]
        request_id = fields.get("request_id")
        if request_id:
            return [line for line, obj in parsed if obj and obj.get("request_id") == request_id]
        service = fields.get("service_name") or chunk.service_name
        same = [line for line, obj in parsed if obj and (obj.get("service_name") or chunk.service_name) == service]
        return same[-RELATED_FALLBACK_LINES:]

    @staticmethod
    def _try_parse_json(line: str) -> dict | None:
        try:
            parsed = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None

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
