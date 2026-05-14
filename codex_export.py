#!/usr/bin/env python3
"""codex_export - export local Codex sessions to HTML / Markdown / JSON / CSV.

Codex stores each local thread as JSONL under:

    ~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<session-id>.jsonl

The lightweight title index lives at:

    ~/.codex/session_index.jsonl

This tool turns those records into a portable bundle with rendered transcript
views, structured data, the raw transcript, and snapshots of files/images that
can be inferred from the session.
"""
from __future__ import annotations

import argparse
import csv
import html as html_mod
import json
import os
import re
import shutil
import sys
import textwrap
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from typing import Any, Iterable, Optional
from urllib.parse import quote, unquote, urlparse


HOME = Path.home()


def _expand_path(value: Any) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser()


def _codex_home_candidates() -> list[Path]:
    """Return plausible Codex home directories for the current platform."""
    candidates: list[Path] = []
    env_home = os.environ.get("CODEX_HOME")
    if env_home:
        candidates.append(_expand_path(env_home))
    candidates.append(HOME / ".codex")
    if sys.platform == "win32":
        user_profile = Path(os.environ.get("USERPROFILE") or str(HOME))
        candidates.append(user_profile / ".codex")
        for env_name in ("APPDATA", "LOCALAPPDATA"):
            value = os.environ.get(env_name)
            if value:
                base = Path(value)
                candidates.extend([base / "Codex", base / "codex", base / "OpenAI" / "Codex"])
    elif sys.platform.startswith("linux"):
        xdg_state = os.environ.get("XDG_STATE_HOME")
        xdg_config = os.environ.get("XDG_CONFIG_HOME")
        if xdg_state:
            candidates.append(Path(xdg_state) / "codex")
        if xdg_config:
            candidates.append(Path(xdg_config) / "codex")
        candidates.append(HOME / ".config" / "codex")

    seen: set[str] = set()
    out: list[Path] = []
    for cand in candidates:
        key = str(cand)
        if key in seen:
            continue
        seen.add(key)
        out.append(cand)
    return out


def _default_codex_home() -> Path:
    candidates = _codex_home_candidates()
    if os.environ.get("CODEX_HOME") and candidates:
        return candidates[0]
    for cand in candidates:
        if (cand / "sessions").exists() or (cand / "session_index.jsonl").exists():
            return cand
    return candidates[0] if candidates else HOME / ".codex"


CODEX_HOME = _default_codex_home()
SESSIONS_ROOT = CODEX_HOME / "sessions"
SESSION_INDEX = CODEX_HOME / "session_index.jsonl"
DEFAULT_OUTPUT = Path.cwd() / "exports"
SUPPORTED_FORMATS = ("html", "md", "json", "csv")
TOOL_RESULT_TRUNCATE = 8000
CSV_CELL_MAX_CHARS = 32000
INTERNAL_ROLES = {"developer", "system"}


@dataclass
class CodexSession:
    session_id: str
    transcript_path: Path
    title: str = ""
    cwd: str = ""
    model: str = ""
    effort: str = ""
    originator: str = ""
    source: str = ""
    model_provider: str = ""
    cli_version: str = ""
    approval_policy: str = ""
    sandbox_policy: str = ""
    collaboration_mode: str = ""
    started_at: str = ""
    updated_at: str = ""
    event_count: int = 0
    file_size: int = 0
    first_user_message: str = ""

    @property
    def display_title(self) -> str:
        if self.title:
            return self.title
        if self.first_user_message:
            return _preview(self.first_user_message, 64)
        return f"Codex session {self.session_id[:8]}"

    @property
    def display_when(self) -> str:
        return _fmt_ts(self.updated_at or self.started_at)

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["transcript_path"] = str(self.transcript_path)
        return d


@dataclass
class FlatBlock:
    index: int
    kind: str
    role: str
    timestamp: str
    text: str = ""
    phase: str = ""
    tool_name: str = ""
    call_id: str = ""
    status: str = ""
    tool_input: Any = None
    is_error: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Attachment:
    source: str
    kind: str
    name: str = ""
    bundle_path: str = ""
    exists: bool = False
    size: int = 0

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class TouchedFile:
    absolute_path: str
    relative_path: str
    op: str
    tool_name: str
    call_id: str
    exists: bool = False
    size: int = 0
    bundle_path: str = ""
    recorded_content: Optional[str] = None
    edit_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d.pop("recorded_content", None)
        return d


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except OSError:
        pass
    return rows


def _parse_dt(value: str) -> Optional[datetime]:
    if not value:
        return None
    s = value.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _sort_key(value: str) -> float:
    dt = _parse_dt(value)
    if not dt:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _fmt_ts(value: str) -> str:
    dt = _parse_dt(value)
    if not dt:
        return value or ""
    if dt.tzinfo is not None:
        dt = dt.astimezone()
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _iso_from_mtime(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return ""


def _human_size(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def _preview(text: str, limit: int = 80) -> str:
    one = " ".join((text or "").strip().split())
    if len(one) <= limit:
        return one
    return one[: max(0, limit - 3)] + "..."


def _preview_plain(text: str, limit: int = 80) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text or "")
    text = text.replace("**", "").replace("__", "").replace("`", "")
    return _preview(text, limit)


def _clean_display_user_text(text: str) -> str:
    """Keep only the user-authored request text for the reading view."""
    text = (text or "").strip()
    if not text:
        return ""
    if text.startswith("<environment_context>") and text.endswith("</environment_context>"):
        return ""

    marker = "## My request for Codex:"
    if marker in text:
        before, request = text.split(marker, 1)
        files = _extract_mentioned_files(before)
        if files:
            lines = ["**Files mentioned by the user:**"]
            for name, path in files:
                if name and path:
                    lines.append(f"- `{name}`: `{path}`")
                elif path:
                    lines.append(f"- `{path}`")
            lines.extend(["", request.strip()])
            text = "\n".join(lines).strip()
        else:
            text = request.strip()
    elif text.startswith("# Files mentioned by the user:"):
        # A file-only preamble is useful metadata, but it is not a standalone
        # prompt with its own assistant answer in the reading view.
        return ""

    return text.strip()


def _extract_mentioned_files(text: str) -> list[tuple[str, str]]:
    files: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("## "):
            continue
        body = line[3:].strip()
        if ":" not in body:
            continue
        name, path = body.split(":", 1)
        name = name.strip()
        path = path.strip()
        if path:
            files.append((name, path))
    return files


def _safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _csv_cell(value: str, limit: int = CSV_CELL_MAX_CHARS) -> tuple[str, int, bool]:
    value = value or ""
    if len(value) <= limit:
        return value, len(value), False
    suffix = "\n...[truncated; full content in session.json and transcript.jsonl]"
    keep = max(0, limit - len(suffix))
    return value[:keep] + suffix, len(value), True


def _parse_arguments(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _text_from_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            btype = block.get("type", "")
            text = (
                block.get("text")
                or block.get("message")
                or block.get("content")
                or block.get("output_text")
                or block.get("input_text")
            )
            if isinstance(text, str):
                parts.append(text)
            elif btype in ("input_image", "image", "output_image"):
                label = block.get("image_url") or block.get("path") or block.get("url") or btype
                parts.append(f"[image: {label}]")
            elif block:
                parts.append(json.dumps(block, ensure_ascii=False))
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        text = content.get("text") or content.get("message") or content.get("content")
        if isinstance(text, str):
            return text
        return json.dumps(content, ensure_ascii=False)
    return str(content)


def _read_index(index_path: Path = SESSION_INDEX) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for obj in _read_jsonl(index_path):
        sid = str(obj.get("id") or "")
        if not sid:
            continue
        out[sid] = {
            "title": str(obj.get("thread_name") or ""),
            "updated_at": str(obj.get("updated_at") or ""),
        }
    return out


def _id_from_filename(path: Path) -> str:
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", path.stem)
    if m:
        return m.group(1)
    return path.stem


def _stringify_policy(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if "type" in value and len(value) <= 3:
            return str(value.get("type") or "")
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def inspect_session(path: Path, index: Optional[dict[str, dict[str, str]]] = None) -> CodexSession:
    index = index or {}
    sid = _id_from_filename(path)
    title = ""
    updated_at = ""
    cwd = ""
    model = ""
    effort = ""
    originator = ""
    source = ""
    model_provider = ""
    cli_version = ""
    approval_policy = ""
    sandbox_policy = ""
    collaboration_mode = ""
    started_at = ""
    first_ts = ""
    last_ts = ""
    first_user = ""
    count = 0

    try:
        file_size = path.stat().st_size
    except OSError:
        file_size = 0

    try:
        f = path.open("r", encoding="utf-8")
    except OSError:
        return CodexSession(session_id=sid, transcript_path=path)

    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            count += 1
            ts = str(obj.get("timestamp") or "")
            if ts and not first_ts:
                first_ts = ts
            if ts:
                last_ts = ts
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            typ = obj.get("type")

            if typ == "session_meta":
                sid = str(payload.get("id") or sid)
                started_at = str(payload.get("timestamp") or started_at)
                cwd = str(payload.get("cwd") or cwd)
                originator = str(payload.get("originator") or originator)
                source = str(payload.get("source") or source)
                model_provider = str(payload.get("model_provider") or model_provider)
                cli_version = str(payload.get("cli_version") or cli_version)
                continue

            if typ == "turn_context":
                cwd = str(payload.get("cwd") or cwd)
                model = str(payload.get("model") or model)
                effort = str(payload.get("effort") or effort)
                approval_policy = _stringify_policy(payload.get("approval_policy")) or approval_policy
                sandbox_policy = _stringify_policy(payload.get("sandbox_policy") or payload.get("file_system_sandbox_policy")) or sandbox_policy
                mode = payload.get("collaboration_mode")
                if isinstance(mode, dict):
                    collaboration_mode = str(mode.get("mode") or collaboration_mode)
                elif mode:
                    collaboration_mode = str(mode)
                continue

            if typ == "response_item" and payload.get("type") == "message":
                role = payload.get("role")
                if role == "user" and not first_user:
                    first_user = _text_from_content(payload.get("content"))
                continue

            if typ == "event_msg" and payload.get("type") == "user_message" and not first_user:
                first_user = _text_from_content(payload.get("message") or payload.get("text_elements"))

    if sid in index:
        title = index[sid].get("title", "")
        updated_at = index[sid].get("updated_at", "")
    if not updated_at:
        updated_at = last_ts or _iso_from_mtime(path)
    if not started_at:
        started_at = first_ts or _iso_from_mtime(path)

    return CodexSession(
        session_id=sid,
        transcript_path=path,
        title=title,
        cwd=cwd,
        model=model,
        effort=effort,
        originator=originator,
        source=source,
        model_provider=model_provider,
        cli_version=cli_version,
        approval_policy=approval_policy,
        sandbox_policy=sandbox_policy,
        collaboration_mode=collaboration_mode,
        started_at=started_at,
        updated_at=updated_at,
        event_count=count,
        file_size=file_size,
        first_user_message=first_user,
    )


def discover_sessions(
    sessions_root: Path = SESSIONS_ROOT,
    index_path: Path = SESSION_INDEX,
) -> list[CodexSession]:
    index = _read_index(index_path)
    if not sessions_root.exists():
        return []
    files = sorted(sessions_root.glob("*/*/*/*.jsonl"))
    sessions = [inspect_session(p, index) for p in files]
    sessions.sort(key=lambda s: _sort_key(s.updated_at or s.started_at), reverse=True)
    return sessions


def resolve_sessions(selector: str, sessions: list[CodexSession]) -> list[CodexSession]:
    if selector == "all":
        return sessions
    if selector == "latest":
        return sessions[:1]
    direct = _expand_path(selector)
    if direct.exists() and direct.is_file():
        return [inspect_session(direct.resolve())]
    exact = [s for s in sessions if s.session_id == selector]
    if exact:
        return exact
    by_prefix = [s for s in sessions if s.session_id.startswith(selector)]
    if by_prefix:
        return by_prefix
    selector_l = selector.lower()
    return [s for s in sessions if selector_l in s.display_title.lower()]


def load_transcript(session: CodexSession) -> tuple[CodexSession, list[dict[str, Any]]]:
    raw = _read_jsonl(session.transcript_path)
    refreshed = inspect_session(session.transcript_path, {session.session_id: {"title": session.title, "updated_at": session.updated_at}})
    return refreshed, raw


def _push_block(blocks: list[FlatBlock], **kwargs: Any) -> None:
    blocks.append(FlatBlock(index=len(blocks), **kwargs))


def flatten(
    raw: list[dict[str, Any]],
    include_context: bool = False,
    include_reasoning: bool = False,
    display_user_inputs: bool = False,
) -> list[FlatBlock]:
    blocks: list[FlatBlock] = []
    seen_text: set[tuple[str, str, str]] = set()
    has_event_user_messages = any(
        obj.get("type") == "event_msg"
        and isinstance(obj.get("payload"), dict)
        and obj["payload"].get("type") == "user_message"
        for obj in raw
    )

    def push_text(role: str, timestamp: str, text: str, phase: str = "", kind: str = "text", extra: Optional[dict[str, Any]] = None) -> None:
        text = text or ""
        sig = (role, phase, text.strip())
        if kind == "text" and sig[2] and sig in seen_text:
            return
        if kind == "text" and sig[2]:
            seen_text.add(sig)
        _push_block(blocks, kind=kind, role=role, timestamp=timestamp, text=text, phase=phase, extra=extra or {})

    for obj in raw:
        typ = obj.get("type")
        timestamp = str(obj.get("timestamp") or "")
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}

        if typ == "session_meta":
            if include_context:
                context = {k: v for k, v in payload.items() if k not in ("base_instructions", "dynamic_tools")}
                push_text("system", timestamp, _safe_json(context), kind="context")
            continue

        if typ == "turn_context":
            if include_context:
                context = {k: v for k, v in payload.items() if k not in ("developer_instructions", "summary")}
                push_text("system", timestamp, _safe_json(context), kind="context")
            continue

        if typ == "event_msg":
            ptype = payload.get("type")
            if ptype in ("token_count", "task_started"):
                continue
            if ptype == "user_message":
                text = _text_from_content(payload.get("message") or payload.get("text_elements"))
                if display_user_inputs:
                    text = _clean_display_user_text(text)
                    if not text:
                        continue
                push_text("user", timestamp, text)
                continue
            if ptype == "agent_message":
                text = _text_from_content(payload.get("message"))
                push_text("assistant", timestamp, text, phase=str(payload.get("phase") or ""))
                continue
            if ptype == "task_complete":
                if include_context:
                    text = _text_from_content(payload.get("last_agent_message"))
                    push_text("assistant", timestamp, text, phase="task_complete")
                continue
            if ptype == "web_search_end":
                action = payload.get("action") or {}
                query = payload.get("query") or ""
                url = action.get("url") if isinstance(action, dict) else ""
                text = f"{query}\n{url}".strip()
                _push_block(
                    blocks,
                    kind="web_search",
                    role="tool",
                    timestamp=timestamp,
                    text=text,
                    tool_name="web_search",
                    call_id=str(payload.get("call_id") or ""),
                    status="completed",
                    extra={"payload": payload},
                )
                continue
            if ptype == "mcp_tool_call_end":
                invocation = payload.get("invocation") if isinstance(payload.get("invocation"), dict) else {}
                result = payload.get("result")
                tool = invocation.get("tool") or "mcp_tool"
                call_id = str(payload.get("call_id") or "")
                is_error = False
                result_text = _text_from_mcp_result(result)
                if isinstance(result, dict) and "Err" in result:
                    is_error = True
                _push_block(
                    blocks,
                    kind="tool_result",
                    role="tool",
                    timestamp=timestamp,
                    text=result_text,
                    tool_name=str(tool),
                    call_id=call_id,
                    is_error=is_error,
                    extra={"mcp_invocation": invocation},
                )
                continue
            if include_context:
                _push_block(blocks, kind="event", role="system", timestamp=timestamp, text=_safe_json(payload), extra={"event_type": ptype})
            continue

        if typ != "response_item":
            if include_context:
                _push_block(blocks, kind="event", role="system", timestamp=timestamp, text=_safe_json(obj), extra={"event_type": typ})
            continue

        ptype = payload.get("type")
        if ptype == "message":
            role = str(payload.get("role") or "")
            phase = str(payload.get("phase") or "")
            text = _text_from_content(payload.get("content"))
            if display_user_inputs and role == "user" and has_event_user_messages:
                continue
            if display_user_inputs and role == "user":
                text = _clean_display_user_text(text)
                if not text:
                    continue
            if role in INTERNAL_ROLES and not include_context:
                continue
            push_text(role or "message", timestamp, text, phase=phase, kind="context" if role in INTERNAL_ROLES else "text")
        elif ptype == "reasoning":
            if not include_reasoning:
                continue
            text = _text_from_reasoning(payload)
            if text:
                _push_block(blocks, kind="reasoning", role="assistant", timestamp=timestamp, text=text)
        elif ptype == "function_call":
            args = _parse_arguments(payload.get("arguments"))
            _push_block(
                blocks,
                kind="tool_call",
                role="assistant",
                timestamp=timestamp,
                tool_name=str(payload.get("name") or ""),
                call_id=str(payload.get("call_id") or ""),
                tool_input=args,
            )
        elif ptype == "function_call_output":
            out = _text_from_content(payload.get("output"))
            is_error = _looks_like_error(out)
            _push_block(
                blocks,
                kind="tool_result",
                role="tool",
                timestamp=timestamp,
                text=out,
                call_id=str(payload.get("call_id") or ""),
                is_error=is_error,
            )
        elif ptype == "web_search_call":
            action = payload.get("action") or {}
            url = action.get("url") if isinstance(action, dict) else ""
            _push_block(
                blocks,
                kind="web_search",
                role="assistant",
                timestamp=timestamp,
                text=str(url or payload.get("status") or ""),
                tool_name="web_search",
                status=str(payload.get("status") or ""),
                extra={"payload": payload},
            )
        elif include_context:
            _push_block(blocks, kind="event", role="system", timestamp=timestamp, text=_safe_json(payload), extra={"event_type": ptype})

    # Fill tool names on outputs from earlier calls when call ids match.
    call_names = {b.call_id: b.tool_name for b in blocks if b.kind == "tool_call" and b.call_id and b.tool_name}
    for b in blocks:
        if b.kind == "tool_result" and not b.tool_name and b.call_id in call_names:
            b.tool_name = call_names[b.call_id]
    return blocks


def _text_from_reasoning(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    summary = payload.get("summary")
    if isinstance(summary, list):
        for item in summary:
            text = _text_from_content(item)
            if text:
                parts.append(text)
    elif isinstance(summary, str):
        parts.append(summary)
    content = payload.get("content")
    text = _text_from_content(content)
    if text:
        parts.append(text)
    return "\n".join(p for p in parts if p)


def _text_from_mcp_result(result: Any) -> str:
    if not isinstance(result, dict):
        return _text_from_content(result)
    payload = result.get("Ok") if "Ok" in result else result.get("Err")
    if isinstance(payload, dict):
        content = payload.get("content")
        text = _text_from_content(content)
        if text:
            return text
    return json.dumps(result, ensure_ascii=False, indent=2)


def _looks_like_error(text: str) -> bool:
    if not text:
        return False
    patterns = [
        r"Process exited with code ([1-9][0-9]*)",
        r"\berror:",
        r"\bfatal:",
        r"Traceback \(most recent call last\)",
    ]
    lower = text.lower()
    return any(re.search(p, lower, flags=re.IGNORECASE) for p in patterns)


def _is_windows_drive_path(value: str) -> bool:
    return bool(re.match(r"^[a-zA-Z]:", value or ""))


def _is_external_uri(value: str) -> bool:
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*):", value or "")
    if not m:
        return False
    scheme = m.group(1).lower()
    if len(scheme) == 1 and _is_windows_drive_path(value):
        return False
    return scheme != "file"


def _local_path_text(path_text: str) -> str:
    if not path_text.lower().startswith("file:"):
        return path_text
    parsed = urlparse(path_text)
    path = unquote(parsed.path or "")
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        path = f"//{parsed.netloc}{path}"
    if re.match(r"^/[a-zA-Z]:", path):
        path = path[1:]
    if sys.platform == "win32":
        path = path.replace("/", "\\")
    return path


def _resolve_path(path_text: str, base: str = "") -> Path:
    p = _expand_path(_local_path_text(path_text))
    if not p.is_absolute():
        p = (_expand_path(base) if base else Path.cwd()) / p
    try:
        return p.resolve(strict=False)
    except OSError:
        return p


def _bundle_rel_for_path(path: Path, cwd: str = "") -> str:
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        resolved = path
    if cwd:
        try:
            base = _expand_path(cwd).resolve(strict=False)
            rel = resolved.relative_to(base)
            return _clean_rel(rel)
        except (ValueError, OSError):
            pass
    if not resolved.is_absolute():
        return _clean_rel(resolved)
    return _clean_rel(Path("absolute") / Path(*resolved.parts[1:]))


def _clean_rel(path: Path) -> str:
    parts = [p for p in path.parts if p not in ("", ".", "..")]
    if not parts:
        return "file"
    return "/".join(parts)


def collect_input_files(raw: list[dict[str, Any]], cwd: str) -> list[Attachment]:
    seen: set[str] = set()
    out: list[Attachment] = []

    def add(value: Any, kind: str, display_name: str = "") -> None:
        if value is None:
            return
        candidates: list[tuple[str, str]] = []
        if isinstance(value, str):
            candidates.append((value, display_name))
        elif isinstance(value, dict):
            for key in ("path", "file", "url", "image_url"):
                if isinstance(value.get(key), str):
                    candidates.append((value[key], display_name or str(value.get("name") or "")))
        elif isinstance(value, list):
            for item in value:
                add(item, kind)
            return
        for raw_path, name in candidates:
            if not raw_path or raw_path in seen:
                continue
            seen.add(raw_path)
            if _is_external_uri(raw_path):
                out.append(Attachment(source=raw_path, kind=kind, name=name, exists=False))
                continue
            p = _resolve_path(raw_path, cwd)
            exists = p.exists()
            size = p.stat().st_size if exists and p.is_file() else 0
            out.append(Attachment(source=str(p), kind=kind, name=name or p.name, exists=exists, size=size))

    for obj in raw:
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        if obj.get("type") in ("event_msg", "response_item"):
            text = ""
            if obj.get("type") == "event_msg" and payload.get("type") == "user_message":
                text = _text_from_content(payload.get("message") or payload.get("text_elements"))
            elif obj.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
                text = _text_from_content(payload.get("content"))
            if text and "## My request for Codex:" in text:
                before = text.split("## My request for Codex:", 1)[0]
                for name, path in _extract_mentioned_files(before):
                    add(path, "mentioned_file", name)
        if obj.get("type") == "event_msg" and payload.get("type") == "user_message":
            add(payload.get("local_images"), "local_image")
            add(payload.get("images"), "image")
        if obj.get("type") == "response_item":
            ptype = payload.get("type")
            if ptype == "message":
                for block in payload.get("content") or []:
                    if isinstance(block, dict) and block.get("type") in ("input_image", "image", "output_image"):
                        add(block, str(block.get("type")))
    return out


def collect_output_files(raw: list[dict[str, Any]], cwd: str) -> list[Attachment]:
    seen: set[str] = set()
    out: list[Attachment] = []

    def add(path_text: Any, kind: str, display_name: str = "") -> None:
        if not isinstance(path_text, str) or not path_text:
            return
        if _is_external_uri(path_text):
            return
        p = _resolve_path(path_text, cwd)
        key = str(p)
        if key in seen:
            return
        seen.add(key)
        exists = p.exists()
        size = p.stat().st_size if exists and p.is_file() else 0
        out.append(Attachment(source=str(p), kind=kind, name=display_name or p.name, exists=exists, size=size))

    for obj in raw:
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        if obj.get("type") == "response_item" and payload.get("type") == "function_call" and payload.get("name") == "view_image":
            args = _parse_arguments(payload.get("arguments"))
            if isinstance(args, dict):
                add(args.get("path"), "view_image")
    return out


def collect_touched_files(blocks: list[FlatBlock], cwd: str) -> list[TouchedFile]:
    touched: dict[tuple[str, str], TouchedFile] = {}

    def add(path_text: str, op: str, tool_name: str, call_id: str, base: str = "", recorded: Optional[str] = None, edit_only: bool = False) -> None:
        if not path_text:
            return
        p = _resolve_path(path_text, base or cwd)
        rel = _bundle_rel_for_path(p, cwd)
        exists = p.exists()
        size = p.stat().st_size if exists and p.is_file() else 0
        key = (str(p), op)
        touched[key] = TouchedFile(
            absolute_path=str(p),
            relative_path=rel,
            op=op,
            tool_name=tool_name,
            call_id=call_id,
            exists=exists,
            size=size,
            recorded_content=recorded,
            edit_only=edit_only,
        )

    for block in blocks:
        if block.kind != "tool_call":
            continue
        args = block.tool_input
        tool = block.tool_name
        if tool == "apply_patch":
            patch = args.get("patch") if isinstance(args, dict) else args
            for item in _parse_patch_touched(str(patch or "")):
                add(item["path"], item["op"], tool, block.call_id, cwd, item.get("content"), item.get("edit_only", False))
        elif tool == "exec_command" and isinstance(args, dict):
            cmd = str(args.get("cmd") or "")
            workdir = str(args.get("workdir") or cwd)
            if "*** Begin Patch" in cmd:
                for item in _parse_patch_touched(cmd):
                    add(item["path"], item["op"], tool, block.call_id, workdir, item.get("content"), item.get("edit_only", False))
        elif tool in ("write_file", "create_file", "str_replace_editor"):
            if isinstance(args, dict):
                path_text = str(args.get("path") or args.get("file_path") or args.get("target_file") or "")
                content = args.get("content") if isinstance(args.get("content"), str) else None
                add(path_text, tool, tool, block.call_id, cwd, content, content is None)
    return sorted(touched.values(), key=lambda t: (t.relative_path, t.op))


def _parse_patch_touched(patch: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    lines = patch.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"\*\*\* (Add|Update|Delete) File: (.+)$", line)
        if not m:
            i += 1
            continue
        action = m.group(1)
        path = m.group(2).strip()
        op = {"Add": "add", "Update": "update", "Delete": "delete"}[action]
        content_lines: list[str] = []
        j = i + 1
        edit_only = op != "add"
        while j < len(lines):
            nxt = lines[j]
            if nxt.startswith("*** Add File: ") or nxt.startswith("*** Update File: ") or nxt.startswith("*** Delete File: ") or nxt == "*** End Patch":
                break
            mmove = re.match(r"\*\*\* Move to: (.+)$", nxt)
            if mmove:
                items.append({"path": path, "op": "move-from", "edit_only": True})
                path = mmove.group(1).strip()
                op = "move-to"
            elif action == "Add" and nxt.startswith("+"):
                content_lines.append(nxt[1:])
                edit_only = False
            j += 1
        items.append({
            "path": path,
            "op": op,
            "content": "\n".join(content_lines) + ("\n" if content_lines else ""),
            "edit_only": edit_only,
        })
        i = j
    return items


def list_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    out: list[Path] = []
    for p in root.rglob("*"):
        if p.is_file():
            out.append(p)
    return sorted(out)


def copy_attachments(attachments: list[Attachment], target: Path, folder: str) -> list[Attachment]:
    if attachments:
        (target / folder).mkdir(parents=True, exist_ok=True)
    used: dict[str, int] = {}
    copied: list[Attachment] = []
    for att in attachments:
        item = Attachment(source=att.source, kind=att.kind, name=att.name, exists=att.exists, size=att.size)
        if not att.exists:
            copied.append(item)
            continue
        src = Path(att.source)
        name = att.name or src.name or "file"
        name = Path(name).name
        count = used.get(name, 0)
        used[name] = count + 1
        if count:
            stem = src.stem or "attachment"
            suffix = src.suffix
            name = f"{stem}-{count + 1}{suffix}"
        dest = target / folder / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dest)
            item.bundle_path = str(dest.relative_to(target))
        except OSError:
            pass
        copied.append(item)
    if attachments:
        manifest = target / folder / "_manifest.json"
        manifest.write_text(
            json.dumps([item.to_dict() for item in copied], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return copied


def copy_touched_files(files: list[TouchedFile], target: Path) -> list[TouchedFile]:
    out: list[TouchedFile] = []
    for tf in files:
        item = TouchedFile(**tf.__dict__)
        dest = target / "outputs" / tf.relative_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        copied = False
        src = Path(tf.absolute_path)
        if tf.exists and src.is_file():
            try:
                shutil.copy2(src, dest)
                copied = True
            except OSError:
                copied = False
        if not copied and tf.recorded_content is not None and tf.recorded_content:
            try:
                dest.write_text(tf.recorded_content, encoding="utf-8")
                copied = True
            except OSError:
                copied = False
        if copied:
            item.bundle_path = str(dest.relative_to(target))
        out.append(item)
    return out


def _href(path: str) -> str:
    return "/".join(quote(part) for part in Path(path).parts)


def _bundle_reference_map(
    inputs: list[Attachment],
    output_refs: list[Attachment],
    files: list[TouchedFile],
) -> dict[str, str]:
    refs: dict[str, str] = {}
    for att in inputs + output_refs:
        if att.source and att.bundle_path:
            refs[att.source] = att.bundle_path
    for f in files:
        if f.absolute_path and f.bundle_path:
            refs[f.absolute_path] = f.bundle_path
    return refs


def _rewrite_bundle_file_links(text: str, refs: dict[str, str]) -> str:
    if not text or not refs:
        return text
    out = text
    for source, bundle_path in sorted(refs.items(), key=lambda item: len(item[0]), reverse=True):
        href = _href(bundle_path)
        label = Path(bundle_path).name or bundle_path
        link = f"[{label}]({href})"
        target_variants = {
            source,
            quote(source),
            quote(source, safe="/:"),
            source.replace(" ", "%20"),
        }
        for target in sorted(target_variants, key=len, reverse=True):
            out = out.replace(f"]({target})", f"]({href})")
            out = out.replace(f"](<{target}>)", f"]({href})")
        out = out.replace(f"`{source}`", link)
        out = out.replace(source, link)
    return out


def _blocks_for_render(
    blocks: list[FlatBlock],
    inputs: list[Attachment],
    output_refs: list[Attachment],
    files: list[TouchedFile],
) -> list[FlatBlock]:
    refs = _bundle_reference_map(inputs, output_refs, files)
    if not refs:
        return blocks
    rendered: list[FlatBlock] = []
    for b in blocks:
        if b.kind in ("text", "reasoning") and b.role in ("user", "assistant"):
            rendered.append(replace(b, text=_rewrite_bundle_file_links(b.text, refs)))
        else:
            rendered.append(b)
    return rendered


def _attachment_label(att: Attachment) -> str:
    if att.bundle_path:
        return Path(att.bundle_path).name
    if att.name:
        return att.name
    return Path(att.source).name or att.source


def render_markdown(
    meta: CodexSession,
    blocks: list[FlatBlock],
    inputs: list[Attachment],
    output_refs: list[Attachment],
    files: list[TouchedFile],
) -> str:
    blocks = _blocks_for_render(blocks, inputs, output_refs, files)
    title = meta.display_title
    out: list[str] = [f"# {title}", ""]
    fields = [
        ("Session ID", meta.session_id, True),
        ("Source", "Codex", False),
        ("Originator", meta.originator, False),
        ("Codex source", meta.source, False),
        ("Model", meta.model, False),
        ("Effort", meta.effort, False),
        ("Working dir", meta.cwd, True),
        ("Started", _fmt_ts(meta.started_at), False),
        ("Updated", _fmt_ts(meta.updated_at), False),
        ("CLI version", meta.cli_version, True),
        ("Approval", meta.approval_policy, True),
        ("Sandbox", meta.sandbox_policy, True),
        ("Collaboration", meta.collaboration_mode, False),
    ]
    for label, value, mono in fields:
        if value:
            out.append(f"- **{label}:** `{value}`" if mono else f"- **{label}:** {value}")
    out.append("")

    if inputs:
        out += [f"## Inputs ({len(inputs)})", ""]
        for att in inputs:
            if att.bundle_path:
                out.append(f"- [{_attachment_label(att)}]({_href(att.bundle_path)}) - {att.kind}, {_human_size(att.size)}")
            else:
                suffix = "missing/local unavailable" if not att.exists else _human_size(att.size)
                out.append(f"- `{att.source}` - {att.kind}, {suffix}")
        out.append("")

    n_outputs = len(output_refs) + len(files)
    if n_outputs:
        out += [f"## Outputs ({n_outputs})", ""]
        for att in output_refs:
            if att.bundle_path:
                out.append(f"- [{_attachment_label(att)}]({_href(att.bundle_path)}) - {att.kind}, {_human_size(att.size)}")
            else:
                suffix = "missing/local unavailable" if not att.exists else _human_size(att.size)
                out.append(f"- `{att.source}` - {att.kind}, {suffix}")
        for f in files:
            if f.bundle_path:
                out.append(f"- `{f.op}` - [{f.relative_path}]({_href(f.bundle_path)})")
            else:
                status = "not readable" if f.edit_only else "missing"
                out.append(f"- `{f.op}` - `{f.absolute_path}` ({status})")
        out.append("")

    out += ["## Transcript", ""]
    for block in blocks:
        ts = _fmt_ts(block.timestamp)
        label = block.role.capitalize() if block.role else block.kind
        if block.phase:
            label += f" / {block.phase}"
        if block.kind in ("text", "context"):
            out += [f"### {label} - {ts}", "", block.text.rstrip() or "_(empty)_", ""]
        elif block.kind == "reasoning":
            out += [f"<details><summary>Reasoning - {ts}</summary>", "", block.text.rstrip(), "", "</details>", ""]
        elif block.kind == "tool_call":
            out += [f"#### Tool call: `{block.tool_name}` - {ts}", "", "```json", _safe_json(block.tool_input), "```", ""]
        elif block.kind == "tool_result":
            text = block.text or ""
            note = ""
            if len(text) > TOOL_RESULT_TRUNCATE:
                note = f"\n\n_...truncated, full text in session.json ({len(text)} chars)_"
                text = text[:TOOL_RESULT_TRUNCATE]
            tag = "Tool error" if block.is_error else "Tool result"
            name = f": `{block.tool_name}`" if block.tool_name else ""
            out += [f"<details><summary>{tag}{name} - {ts}</summary>", "", "```", text.rstrip(), "```"]
            if note:
                out.append(note)
            out += ["", "</details>", ""]
        elif block.kind == "web_search":
            out += [f"#### Web search - {ts}", "", "```", block.text.rstrip(), "```", ""]
        else:
            out += [f"#### {block.kind} - {ts}", "", "```json", block.text.rstrip(), "```", ""]
    return "\n".join(out)


HTML_TEMPLATE = Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title</title>
<script src="https://cdn.jsdelivr.net/npm/marked@12/marked.min.js"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11/build/styles/github.min.css">
<script src="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11/build/highlight.min.js"></script>
<style>
:root {
  color-scheme: light dark;
  --bg: #f4f6fa; --fg: #1f2937; --muted: #667085; --faint: #8a94a6;
  --border: #d8dee8; --panel: #ffffff; --panel-soft: #f8fafc;
  --turn-bg: #ffffff; --turn-bd: #cfd7e3;
  --user-bg: #f8fafc; --user-bd: #d7dee8;
  --asst-bg: #ffffff; --asst-bd: #d8dee8;
  --process-bg: #f5f7fb; --process-bd: #d8dee8;
  --tool-bg: #f8fafc; --tool-bd: #d8dee8;
  --result-bg: #f7f9fc; --result-bd: #d8dee8;
  --result-err-bg: #fff1f2; --result-err-bd: #fda4af;
  --context-bg: #f2f4f7; --context-bd: #d8dee8;
  --code-bg: #111827; --code-fg: #f3f4f6;
  --shadow: 0 1px 2px rgba(16,24,40,.04), 0 8px 24px rgba(16,24,40,.06);
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: var(--bg); color: var(--fg);
  font: 15px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Helvetica Neue", Arial, sans-serif;
}
a { color: #2563eb; text-decoration: none; }
a:hover { text-decoration: underline; }
.wrap { max-width: 1060px; margin: 32px auto; padding: 0 20px 56px; }
.wrap > *, .transcript, .exchange, .prompt-group, .response-group, .turn,
.turn-body, .assistant-stack, .answer-final, .msg, details.process {
  width: 100%; max-width: 100%; min-width: 0;
}
.theme-toggle {
  position: fixed; top: 16px; right: 18px; z-index: 20;
  border: 1px solid var(--border); border-radius: 999px; padding: 7px 11px;
  background: var(--panel); color: var(--fg); font: 12px/1.2 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  box-shadow: var(--shadow); cursor: pointer;
}
.theme-toggle:hover { border-color: var(--faint); }
.transcript { display: grid; gap: 24px; }
.head, .section, .toc {
  background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
}
.head { padding: 20px 24px; margin-bottom: 22px; box-shadow: var(--shadow); }
.head h1 { margin: 0 0 12px; font-size: 22px; line-height: 1.3; }
.head dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px 16px; margin: 0; font-size: 13px; color: var(--muted); }
.head dt { font-weight: 700; }
.head dd { margin: 0; word-break: break-word; }
.mono, code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.section, .toc { padding: 14px 20px; margin-bottom: 22px; box-shadow: 0 1px 2px rgba(16,24,40,.03); }
.section h2, .toc h2 { margin: 0 0 10px; font-size: 14px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }
.section ul, .toc ol { margin: 0; padding-left: 22px; font-size: 13px; }
.section li { margin: 3px 0; }
.exchange {
  border: 1px solid var(--turn-bd); border-radius: 14px; padding: 18px 20px;
  margin-bottom: 24px; background: var(--turn-bg); box-shadow: var(--shadow);
}
.prompt-group { display: grid; gap: 18px; }
.prompt-group + .response-group { margin-top: 14px; }
.response-group {
  border: 1px solid var(--process-bd); border-radius: 12px; padding: 10px;
  background: var(--process-bg); display: grid; gap: 12px;
}
.answer-head {
  display: flex; justify-content: space-between; gap: 12px; align-items: center;
  padding: 2px 2px 0; color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
}
.answer-head strong { color: var(--fg); }
.answer-final { display: grid; gap: 12px; }
.turn {
  border: 1px solid var(--turn-bd); border-radius: 12px; padding: 14px;
  margin: 0; background: var(--panel-soft);
}
.turn-head {
  display: flex; align-items: baseline; gap: 12px; padding-bottom: 8px; margin-bottom: 14px;
  border-bottom: 1px dashed var(--border); color: var(--muted); font-size: 12px;
}
.turn-num { flex: 0 0 auto; color: var(--fg); background: rgba(9,105,218,.08); border-radius: 999px; padding: 2px 8px; font-weight: 700; font-size: 11px; }
.turn-title { flex: 1 1 auto; min-width: 0; color: var(--fg); font-weight: 500; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.turn-ts { flex: 0 0 auto; font-variant-numeric: tabular-nums; }
.turn-body { display: grid; gap: 12px; }
.assistant-stack { display: grid; gap: 12px; }
.msg { border: 1px solid; border-radius: 10px; padding: 14px 18px; margin: 0; overflow: hidden; color: var(--fg); background: var(--panel); }
.msg:last-child { margin-bottom: 0; }
.msg.user { background: var(--user-bg); border-color: var(--user-bd); }
.msg.assistant { background: var(--asst-bg); border-color: var(--asst-bd); }
.msg.context, .msg.web_search, .msg.event { background: var(--context-bg); border-color: var(--context-bd); color: var(--fg); font-size: 13px; }
.msg.reasoning { background: #f3e8ff; border-color: #c8a2f5; border-style: dashed; }
.msg.tool_call { background: var(--tool-bg); border-color: var(--tool-bd); }
.msg.tool_result { background: var(--result-bg); border-color: var(--result-bd); }
.msg.tool_result.error { background: var(--result-err-bg); border-color: var(--result-err-bd); }
.msg-head {
  display: flex; justify-content: space-between; gap: 12px; margin-bottom: 10px;
  color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
}
.msg-head strong { color: var(--fg); }
.msg-head span { color: var(--muted); font-variant-numeric: tabular-nums; }
.md > *:first-child { margin-top: 0; }
.md > *:last-child { margin-bottom: 0; }
.md p, .md ul, .md ol, .md blockquote { margin: .5em 0; }
.md h1, .md h2, .md h3, .md h4 { margin: .6em 0 .3em; }
.md ul, .md ol { padding-left: 1.6em; }
.md { overflow-wrap: anywhere; }
.md pre, pre.raw {
  background: var(--code-bg); color: var(--code-fg); border-radius: 7px; padding: 12px 14px;
  overflow-x: auto; max-height: 520px; white-space: pre-wrap; word-break: break-word; margin: .5em 0;
}
.md :not(pre) > code { background: rgba(100,116,139,.16); border-radius: 4px; padding: 1px 4px; }
details.process {
  border: 1px solid var(--process-bd); border-radius: 9px; padding: 10px 14px;
  margin: 0; background: var(--process-bg);
}
details.process > summary { cursor: pointer; color: var(--muted); font-weight: 700; user-select: none; }
details.process[open] > summary { color: var(--fg); margin-bottom: 8px; }
.process-body { display: grid; gap: 12px; }
.truncated { color: var(--muted); font-size: 12px; font-style: italic; margin-top: 6px; }
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f141b; --fg: #e6edf3; --muted: #a3acba; --faint: #818998;
    --border: #303844; --panel: #141a23; --panel-soft: #111821;
    --turn-bg: #111821; --turn-bd: #303844;
    --user-bg: #161f2b; --user-bd: #3a4656;
    --asst-bg: #141a23; --asst-bd: #303844;
    --process-bg: #121923; --process-bd: #303844;
    --tool-bg: #131b25; --tool-bd: #303844;
    --result-bg: #131b25; --result-bd: #303844;
    --result-err-bg: #301820; --result-err-bd: #7f1d1d;
    --context-bg: #121923; --context-bd: #303844;
    --code-bg: #05080f; --code-fg: #e6edf3;
    --shadow: 0 1px 2px rgba(0,0,0,.3), 0 8px 24px rgba(0,0,0,.2);
  }
  a { color: #7ab7ff; }
  .turn-num { background: rgba(122,183,255,.16); }
  .msg.reasoning { background: #2d1b4e; border-color: #8957e5; }
}
:root[data-theme="dark"] {
  --bg: #0f141b; --fg: #e6edf3; --muted: #a3acba; --faint: #818998;
  --border: #303844; --panel: #141a23; --panel-soft: #111821;
  --turn-bg: #111821; --turn-bd: #303844;
  --user-bg: #161f2b; --user-bd: #3a4656;
  --asst-bg: #141a23; --asst-bd: #303844;
  --process-bg: #121923; --process-bd: #303844;
  --tool-bg: #131b25; --tool-bd: #303844;
  --result-bg: #131b25; --result-bd: #303844;
  --result-err-bg: #301820; --result-err-bd: #7f1d1d;
  --context-bg: #121923; --context-bd: #303844;
  --code-bg: #05080f; --code-fg: #e6edf3;
  --shadow: 0 1px 2px rgba(0,0,0,.3), 0 8px 24px rgba(0,0,0,.2);
}
:root[data-theme="light"] {
  --bg: #f4f6fa; --fg: #1f2937; --muted: #667085; --faint: #8a94a6;
  --border: #d8dee8; --panel: #ffffff; --panel-soft: #f8fafc;
  --turn-bg: #ffffff; --turn-bd: #cfd7e3;
  --user-bg: #f8fafc; --user-bd: #d7dee8;
  --asst-bg: #ffffff; --asst-bd: #d8dee8;
  --process-bg: #f5f7fb; --process-bd: #d8dee8;
  --tool-bg: #f8fafc; --tool-bd: #d8dee8;
  --result-bg: #f7f9fc; --result-bd: #d8dee8;
  --result-err-bg: #fff1f2; --result-err-bd: #fda4af;
  --context-bg: #f2f4f7; --context-bd: #d8dee8;
  --code-bg: #111827; --code-fg: #f3f4f6;
  --shadow: 0 1px 2px rgba(16,24,40,.04), 0 8px 24px rgba(16,24,40,.06);
}
</style>
</head>
<body>
<button class="theme-toggle" id="themeToggle" type="button" aria-label="Toggle color theme">Theme</button>
<div class="wrap">
$header
$attachments
$files
$toc
$messages
</div>
<script>
(function () {
  var root = document.documentElement;
  var button = document.getElementById("themeToggle");
  var stored = "";
  try { stored = localStorage.getItem("codex-export-theme") || ""; } catch (e) {}
  var preferred = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  function setTheme(theme) {
    root.setAttribute("data-theme", theme);
    if (button) button.textContent = theme === "dark" ? "Light" : "Dark";
    try { localStorage.setItem("codex-export-theme", theme); } catch (e) {}
  }
  setTheme(stored || preferred);
  if (button) {
    button.addEventListener("click", function () {
      setTheme(root.getAttribute("data-theme") === "dark" ? "light" : "dark");
    });
  }
  var opts = { gfm: true, breaks: true, headerIds: false, mangle: false };
  if (window.marked && marked.setOptions) marked.setOptions(opts);
  document.querySelectorAll(".md").forEach(function (el) {
    var raw = el.textContent;
    el.innerHTML = window.marked ? marked.parse(raw, opts) : raw;
  });
  if (window.hljs) {
    document.querySelectorAll("pre code").forEach(function (el) {
      try { hljs.highlightElement(el); } catch (e) {}
    });
  }
})();
</script>
</body>
</html>
""")


def render_html(
    meta: CodexSession,
    blocks: list[FlatBlock],
    inputs: list[Attachment],
    output_refs: list[Attachment],
    files: list[TouchedFile],
) -> str:
    blocks = _blocks_for_render(blocks, inputs, output_refs, files)
    esc = html_mod.escape
    title = meta.display_title
    rows = [
        ("Session ID", meta.session_id, True),
        ("Source", "Codex", False),
        ("Originator", meta.originator, False),
        ("Codex source", meta.source, False),
        ("Model", meta.model, False),
        ("Effort", meta.effort, False),
        ("Working dir", meta.cwd, True),
        ("Started", _fmt_ts(meta.started_at), False),
        ("Updated", _fmt_ts(meta.updated_at), False),
        ("CLI version", meta.cli_version, True),
        ("Approval", meta.approval_policy, True),
        ("Sandbox", meta.sandbox_policy, True),
        ("Collaboration", meta.collaboration_mode, False),
    ]
    head = [f"<header class='head'><h1>{esc(title)}</h1><dl>"]
    for label, value, mono in rows:
        if value:
            cls = " class='mono'" if mono else ""
            head.append(f"<dt>{esc(label)}</dt><dd{cls}>{esc(value)}</dd>")
    head.append("</dl></header>")

    inputs_html = ""
    if inputs:
        items = []
        for att in inputs:
            if att.bundle_path:
                items.append(
                    f"<li><a href='{esc(_href(att.bundle_path), quote=True)}'>{esc(_attachment_label(att))}</a>"
                    f" <span class='mono'>{esc(att.kind)}</span> <span>{esc(_human_size(att.size))}</span></li>"
                )
            else:
                status = "missing/local unavailable" if not att.exists else _human_size(att.size)
                items.append(f"<li><span class='mono'>{esc(att.source)}</span> {esc(att.kind)} {esc(status)}</li>")
        inputs_html = f"<section class='section'><h2>Inputs ({len(inputs)})</h2><ul>{''.join(items)}</ul></section>"

    outputs_html = ""
    n_outputs = len(output_refs) + len(files)
    if n_outputs:
        items = []
        for att in output_refs:
            if att.bundle_path:
                items.append(
                    f"<li><a href='{esc(_href(att.bundle_path), quote=True)}'>{esc(_attachment_label(att))}</a>"
                    f" <span class='mono'>{esc(att.kind)}</span> <span>{esc(_human_size(att.size))}</span></li>"
                )
            else:
                status = "missing/local unavailable" if not att.exists else _human_size(att.size)
                items.append(f"<li><span class='mono'>{esc(att.source)}</span> {esc(att.kind)} {esc(status)}</li>")
        for f in files:
            if f.bundle_path:
                link = f"<a href='{esc(_href(f.bundle_path), quote=True)}'>{esc(f.relative_path)}</a>"
            else:
                link = f"<span class='mono'>{esc(f.absolute_path)}</span>"
            items.append(f"<li><span class='mono'>{esc(f.op)}</span> {link}</li>")
        outputs_html = f"<section class='section'><h2>Outputs ({n_outputs})</h2><ul>{''.join(items)}</ul></section>"

    preamble, exchanges = _split_exchanges(blocks)
    toc = ""
    user_blocks = [u for ex in exchanges for u in ex["users"]]
    if user_blocks:
        items = []
        for i, u in enumerate(user_blocks):
            preview = _preview_plain(u.text, 90) or "(empty)"
            items.append(f"<li><a href='#u{i}'>{esc(preview)}</a></li>")
        toc = f"<nav class='toc'><h2>User prompts</h2><ol>{''.join(items)}</ol></nav>"

    parts: list[str] = ["<div class='transcript'>"]
    if preamble:
        inner = "".join(_render_block_html(b) for b in preamble)
        parts.append(f"<details class='process'><summary>Pre-conversation events ({len(preamble)})</summary>{inner}</details>")

    user_i = 0
    for ex_i, exchange in enumerate(exchanges):
        users: list[FlatBlock] = exchange["users"]
        body: list[FlatBlock] = exchange["body"]
        hidden = [b for b in body if b.kind not in ("text", "context")]
        visible = [b for b in body if b.kind in ("text", "context")]

        parts.append(f"<section class='exchange' id='e{ex_i}'>")
        parts.append("<div class='prompt-group'>")
        for user in users:
            preview = _preview_plain(user.text, 120) or "(empty)"
            ts = esc(_fmt_ts(user.timestamp))
            parts.append(f"<section class='turn prompt-turn' id='u{user_i}'>")
            parts.append(
                f"<div class='turn-head'><span class='turn-num'>#{user_i + 1}</span>"
                f"<span class='turn-title'>{esc(preview)}</span><span class='turn-ts'>{ts}</span></div>"
            )
            parts.append("<div class='turn-body'>")
            parts.append(_render_block_html(user))
            parts.append("</div></section>")
            user_i += 1
        parts.append("</div>")

        if hidden or visible:
            parts.append("<section class='response-group'>")
            first_reply = next((b for b in visible if b.timestamp), None)
            answer_ts = esc(_fmt_ts(first_reply.timestamp)) if first_reply else ""
            parts.append(
                "<div class='answer-head'><strong>Assistant answer</strong>"
                f"<span>{answer_ts}</span></div>"
            )
        if hidden:
            n_tools = sum(1 for b in hidden if b.kind == "tool_call")
            n_results = sum(1 for b in hidden if b.kind == "tool_result")
            n_reason = sum(1 for b in hidden if b.kind == "reasoning")
            bits = []
            if n_reason:
                bits.append(f"{n_reason} reasoning")
            if n_tools:
                bits.append(f"{n_tools} calls")
            if n_results:
                bits.append(f"{n_results} results")
            label = " - ".join(bits) if bits else f"{len(hidden)} events"
            inner = "<div class='process-body'>" + "".join(_render_block_html(b) for b in hidden) + "</div>"
            process_title = "Reasoning and tool activity" if n_reason else "Tool activity"
            parts.append(f"<details class='process'><summary>{esc(process_title)} - {esc(label)}</summary>{inner}</details>")
        if visible:
            commentary = [b for b in visible if b.phase != "final_answer"]
            final_answers = [b for b in visible if b.phase == "final_answer"]
            if commentary:
                label = f"{len(commentary)} message{'s' if len(commentary) != 1 else ''}"
                parts.append(
                    "<details class='process assistant-commentary'>"
                    f"<summary>Assistant commentary - {esc(label)}</summary>"
                    "<div class='process-body assistant-stack'>"
                )
                for b in commentary:
                    parts.append(_render_block_html(b))
                parts.append("</div></details>")
            if final_answers:
                parts.append("<div class='answer-final'>")
                for b in final_answers:
                    parts.append(_render_block_html(b))
                parts.append("</div>")
        if hidden or visible:
            parts.append("</section>")
        parts.append("</section>")
    parts.append("</div>")

    return HTML_TEMPLATE.substitute(
        title=esc(title),
        header="".join(head),
        attachments=inputs_html,
        files=outputs_html,
        toc=toc,
        messages="".join(parts),
    )


def _split_exchanges(blocks: list[FlatBlock]) -> tuple[list[FlatBlock], list[dict[str, Any]]]:
    preamble: list[FlatBlock] = []
    exchanges: list[dict[str, Any]] = []
    users: list[FlatBlock] = []
    body: list[FlatBlock] = []

    def flush() -> None:
        nonlocal users, body
        if users:
            exchanges.append({"users": users, "body": body})
        users = []
        body = []

    for b in blocks:
        if b.kind == "text" and b.role == "user":
            if users and body:
                flush()
            users.append(b)
        elif not users:
            preamble.append(b)
        else:
            body.append(b)
    flush()
    return preamble, exchanges


def _render_block_html(block: FlatBlock) -> str:
    esc = html_mod.escape
    ts = esc(_fmt_ts(block.timestamp))
    kind_cls = esc(block.kind)
    if block.kind == "tool_result" and block.is_error:
        kind_cls += " error"
    role = block.role.capitalize() if block.role else block.kind
    if block.phase:
        role += f" / {block.phase}"
    if block.kind in ("text", "context"):
        if block.kind == "text" and block.role in ("user", "assistant"):
            kind_cls = block.role
        return (
            f"<div class='msg {kind_cls}'><div class='msg-head'><strong>{esc(role)}</strong><span>{ts}</span></div>"
            f"<div class='md'>{esc(block.text)}</div></div>"
        )
    if block.kind == "tool_call":
        body = esc(_safe_json(block.tool_input))
        label = f"Tool call: {block.tool_name}" if block.tool_name else "Tool call"
        return (
            f"<div class='msg tool_call'><div class='msg-head'><strong>{esc(label)}</strong><span>{ts}</span></div>"
            f"<pre class='raw'><code>{body}</code></pre></div>"
        )
    if block.kind == "tool_result":
        text = block.text or ""
        note = ""
        if len(text) > TOOL_RESULT_TRUNCATE:
            note = f"<div class='truncated'>...truncated, full text in session.json ({len(text)} chars)</div>"
            text = text[:TOOL_RESULT_TRUNCATE]
        label = "Tool error" if block.is_error else "Tool result"
        if block.tool_name:
            label += f": {block.tool_name}"
        return (
            f"<div class='msg {kind_cls}'><div class='msg-head'><strong>{esc(label)}</strong><span>{ts}</span></div>"
            f"<pre class='raw'>{esc(text.rstrip())}</pre>{note}</div>"
        )
    if block.kind == "reasoning":
        return (
            f"<div class='msg reasoning'><div class='msg-head'><strong>Reasoning</strong><span>{ts}</span></div>"
            f"<div class='md'>{esc(block.text)}</div></div>"
        )
    if block.kind == "web_search":
        return (
            f"<div class='msg web_search'><div class='msg-head'><strong>Web search</strong><span>{ts}</span></div>"
            f"<pre class='raw'>{esc(block.text.rstrip())}</pre></div>"
        )
    return (
        f"<div class='msg event'><div class='msg-head'><strong>{esc(block.kind)}</strong><span>{ts}</span></div>"
        f"<pre class='raw'>{esc(block.text.rstrip())}</pre></div>"
    )


def render_json(
    meta: CodexSession,
    blocks: list[FlatBlock],
    inputs: list[Attachment],
    output_refs: list[Attachment],
    files: list[TouchedFile],
) -> str:
    outputs = [a.to_dict() for a in output_refs] + [f.to_dict() for f in files]
    payload = {
        "meta": meta.to_dict(),
        "inputs": [a.to_dict() for a in inputs],
        "outputs": outputs,
        "attachments": [a.to_dict() for a in inputs],
        "files": outputs,
        "messages": [b.to_dict() for b in blocks],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def write_csv(path: Path, blocks: list[FlatBlock]) -> None:
    cols = [
        "index", "timestamp", "role", "phase", "kind",
        "tool_name", "call_id", "is_error", "preview",
        "content", "content_chars", "content_truncated",
        "tool_input_json", "tool_input_json_chars", "tool_input_json_truncated",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(cols)
        for b in blocks:
            if b.kind == "tool_call":
                content = _safe_json(b.tool_input)
            else:
                content = b.text
            content_cell, content_chars, content_truncated = _csv_cell(content)
            tool_input_json = json.dumps(b.tool_input, ensure_ascii=False) if b.tool_input is not None else ""
            tool_input_cell, tool_input_chars, tool_input_truncated = _csv_cell(tool_input_json)
            writer.writerow([
                b.index,
                b.timestamp,
                b.role,
                b.phase,
                b.kind,
                b.tool_name,
                b.call_id,
                "1" if b.is_error else "",
                _preview(content, 200),
                content_cell,
                content_chars,
                "1" if content_truncated else "",
                tool_input_cell,
                tool_input_chars,
                "1" if tool_input_truncated else "",
            ])


def export_one(
    session: CodexSession,
    output_root: Path,
    formats: Iterable[str],
    include_files: bool,
    include_context: bool,
    include_reasoning: bool,
) -> Optional[Path]:
    if not session.transcript_path.exists():
        print(f"warn: skipping {session.session_id}: transcript not found", file=sys.stderr)
        return None

    meta, raw = load_transcript(session)
    display_blocks = flatten(
        raw,
        include_context=include_context,
        include_reasoning=include_reasoning,
        display_user_inputs=True,
    )
    archive_blocks = flatten(raw, include_context=True, include_reasoning=True)
    inputs = collect_input_files(raw, meta.cwd) if include_files else []
    output_refs = collect_output_files(raw, meta.cwd) if include_files else []
    touched = collect_touched_files(archive_blocks, meta.cwd) if include_files else []

    target = output_root / meta.session_id
    target.mkdir(parents=True, exist_ok=True)
    for folder in ("inputs", "outputs", "attachments", "assets"):
        stale = target / folder
        if stale.exists():
            if stale.is_dir():
                shutil.rmtree(stale)
            else:
                stale.unlink()
    shutil.copy2(meta.transcript_path, target / "transcript.jsonl")

    (target / "metadata.json").write_text(json.dumps(meta.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    copied_inputs = copy_attachments(inputs, target, "inputs") if include_files else []
    copied_output_refs = copy_attachments(output_refs, target, "outputs") if include_files else []
    copied_files = copy_touched_files(touched, target) if include_files else []

    formats = list(formats)
    if "html" in formats:
        (target / "session.html").write_text(render_html(meta, display_blocks, copied_inputs, copied_output_refs, copied_files), encoding="utf-8")
    if "md" in formats:
        (target / "session.md").write_text(render_markdown(meta, display_blocks, copied_inputs, copied_output_refs, copied_files), encoding="utf-8")
    if "json" in formats:
        (target / "session.json").write_text(render_json(meta, archive_blocks, copied_inputs, copied_output_refs, copied_files), encoding="utf-8")
    if "csv" in formats:
        write_csv(target / "session.csv", archive_blocks)

    _write_bundle_readme(target, meta, display_blocks, archive_blocks, copied_inputs, copied_output_refs, copied_files, formats)
    return target


def _write_bundle_readme(
    target: Path,
    meta: CodexSession,
    display_blocks: list[FlatBlock],
    archive_blocks: list[FlatBlock],
    inputs: list[Attachment],
    output_refs: list[Attachment],
    files: list[TouchedFile],
    formats: list[str],
) -> None:
    n_user = sum(1 for b in display_blocks if b.kind == "text" and b.role == "user")
    n_asst = sum(1 for b in display_blocks if b.kind == "text" and b.role == "assistant")
    n_tools = sum(1 for b in display_blocks if b.kind == "tool_call")
    lines = [
        f"# {meta.display_title}",
        "",
        f"- Session ID: `{meta.session_id}`",
        f"- Started: {_fmt_ts(meta.started_at)}",
        f"- Updated: {_fmt_ts(meta.updated_at)}",
        f"- Working dir: `{meta.cwd}`" if meta.cwd else "",
        f"- Model: {meta.model}" if meta.model else "",
        f"- Rendered blocks: {len(display_blocks)} ({n_user} user, {n_asst} assistant, {n_tools} tool calls)",
        f"- Archived blocks: {len(archive_blocks)}",
    ]
    lines = [line for line in lines if line]
    if inputs:
        lines.append(f"- Inputs: {len(inputs)}")
    n_outputs = len(output_refs) + len(files)
    if n_outputs:
        lines.append(f"- Outputs: {n_outputs}")
    lines += ["", "## Files in this bundle", ""]
    if "html" in formats:
        lines.append("- `session.html` - formatted browser view")
    if "md" in formats:
        lines.append("- `session.md` - Markdown export")
    if "json" in formats:
        lines.append("- `session.json` - structured per-block export")
    if "csv" in formats:
        lines.append("- `session.csv` - flat per-block table")
    lines.append("- `metadata.json` - extracted Codex session metadata")
    lines.append("- `transcript.jsonl` - raw Codex JSONL transcript")
    if inputs:
        lines.append("- `inputs/` - readable user-provided or user-mentioned files")
    if n_outputs:
        lines.append("- `outputs/` - readable generated, viewed, written, or edited files")
    (target / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_list(args: argparse.Namespace) -> int:
    codex_home = _expand_path(args.codex_home)
    root = _expand_path(args.sessions_root).resolve() if args.sessions_root else (codex_home / "sessions").resolve()
    index = _expand_path(args.index).resolve() if args.index else (codex_home / "session_index.jsonl").resolve()
    sessions = discover_sessions(root, index)
    if not sessions:
        print(f"No Codex sessions found under {root}")
        return 0
    for s in sessions:
        title = _preview(s.display_title, 70)
        model = f" - {s.model}" if s.model else ""
        cwd = f"\n   cwd: {s.cwd}" if s.cwd else ""
        print(f"{s.session_id}  {s.display_when}  {title}{model}{cwd}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    formats = [f.strip().lower() for f in args.formats.split(",") if f.strip()]
    invalid = [f for f in formats if f not in SUPPORTED_FORMATS]
    if invalid:
        print(f"error: unknown format(s): {', '.join(invalid)}", file=sys.stderr)
        return 2

    codex_home = _expand_path(args.codex_home)
    root = _expand_path(args.sessions_root).resolve() if args.sessions_root else (codex_home / "sessions").resolve()
    index = _expand_path(args.index).resolve() if args.index else (codex_home / "session_index.jsonl").resolve()
    sessions = discover_sessions(root, index)
    targets = resolve_sessions(args.session, sessions)

    if not targets:
        print(f"No Codex session matched '{args.session}'", file=sys.stderr)
        return 1
    if args.session != "all" and len(targets) > 1:
        print(f"Ambiguous selector '{args.session}' matched {len(targets)} sessions:", file=sys.stderr)
        for s in targets:
            print(f"  {s.session_id}  {s.display_title}", file=sys.stderr)
        return 1

    output_root = _expand_path(args.output).resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    exported = 0
    for session in targets:
        target = export_one(
            session=session,
            output_root=output_root,
            formats=formats,
            include_files=not args.no_files,
            include_context=args.include_context,
            include_reasoning=args.include_reasoning,
        )
        if target:
            print(f"exported {session.session_id} -> {target}")
            exported += 1
    return 0 if exported else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="codex-export",
        description="Export local Codex sessions to HTML, Markdown, JSON, and CSV bundles.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              codex-export list
              codex-export export latest
              codex-export export <session-id-prefix> --output ./exports
              codex-export export all --formats html,json
              codex-export export latest --no-files
              codex-export export latest --include-reasoning
              codex-export export ~/.codex/sessions/2026/05/14/rollout-....jsonl
        """),
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--codex-home",
        default=str(CODEX_HOME),
        metavar="DIR",
        help=f"Codex home directory (default: {CODEX_HOME})",
    )
    common.add_argument(
        "--sessions-root",
        default=None,
        metavar="DIR",
        help="override the sessions directory (default: <codex-home>/sessions)",
    )
    common.add_argument(
        "--index",
        default=None,
        metavar="FILE",
        help="override session_index.jsonl (default: <codex-home>/session_index.jsonl)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", parents=[common], help="list available Codex sessions").set_defaults(func=cmd_list)

    pe = sub.add_parser("export", parents=[common], help="export one or more sessions")
    pe.add_argument("session", help="session id prefix, title substring, 'latest', 'all', or a JSONL path")
    pe.add_argument(
        "-o",
        "--output",
        "--out",
        dest="output",
        default=str(DEFAULT_OUTPUT),
        metavar="DIR",
        help=f"directory to write export bundles into (default: {DEFAULT_OUTPUT})",
    )
    pe.add_argument(
        "--formats",
        default=",".join(SUPPORTED_FORMATS),
        help=f"comma-separated subset of {','.join(SUPPORTED_FORMATS)} (default: all)",
    )
    pe.add_argument("--no-files", action="store_true", help="skip copying input/output files")
    pe.add_argument("--include-context", action="store_true", help="include developer/system context blocks in rendered exports")
    pe.add_argument(
        "--include-reasoning",
        action="store_true",
        help="include readable Codex reasoning summary/content blocks; raw transcript always remains lossless",
    )
    pe.set_defaults(func=cmd_export)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
