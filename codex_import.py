#!/usr/bin/env python3
"""codex_import - restore Codex export bundles into a Codex home.

This tool consumes bundles created by codex-export and recreates:

    <codex-home>/sessions/YYYY/MM/DD/rollout-*.jsonl
    <codex-home>/session_index.jsonl

It also restores packaged input/output files into a workspace so paths in the
imported transcript can point at files available on the new machine. It never
copies authentication state; the destination Codex install must use its own
logged-in account.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import textwrap
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import unquote, urlparse


HOME = Path.home()


def _expand_path(value: Any) -> Path:
    text = str(value)
    text = re.sub(
        r"\$env:([A-Za-z_][A-Za-z0-9_]*)",
        lambda m: os.environ.get(m.group(1), m.group(0)),
        text,
    )
    return Path(os.path.expandvars(text)).expanduser()


def _codex_home_candidates() -> list[Path]:
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
DEFAULT_WORKSPACE_ROOT = HOME / "CodexImported"
AUTH_RISK_WARNING = (
    "HIGH RISK: --include-auth restores Codex authentication material from the bundle. "
    "Use it only for same-account cross-device migration. It can grant access to the "
    "source account if copied to the wrong machine."
)
AUTH_CONFIRM_PHRASE = "I UNDERSTAND"


@dataclass
class Bundle:
    root: Path
    metadata: dict[str, Any]
    transcript_path: Path
    raw: list[dict[str, Any]]
    session_id: str
    title: str
    old_cwd: str = ""
    started_at: str = ""
    updated_at: str = ""

    @property
    def display_title(self) -> str:
        return self.title or f"Codex session {self.session_id[:8]}"


@dataclass
class ImportPlan:
    bundle: Bundle
    codex_home: Path
    workspace: Path
    session_id: str
    transcript_dest: Path
    index_path: Path
    replacements: dict[str, str] = field(default_factory=dict)
    input_files: list[tuple[Path, Path]] = field(default_factory=list)
    output_files: list[tuple[Path, Path]] = field(default_factory=list)
    bundle_files: list[tuple[Path, Path]] = field(default_factory=list)
    auth_files: list[tuple[Path, Path, str]] = field(default_factory=list)
    copied_inputs: int = 0
    copied_outputs: int = 0
    copied_bundle_files: int = 0
    copied_auth: int = 0
    wrote_index: bool = False
    wrote_transcript: bool = False
    wrote_workspace: bool = False
    wrote_auth: bool = False
    changed_id: bool = False


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


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
    except OSError as exc:
        raise ValueError(f"cannot read transcript {path}: {exc}") from exc
    return rows


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _parse_dt(value: str) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _fmt_ts(value: str) -> str:
    dt = _parse_dt(value)
    if not dt:
        return value or ""
    if dt.tzinfo is not None:
        dt = dt.astimezone()
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _safe_name(value: str, fallback: str = "codex-session") -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    if not text:
        text = fallback
    text = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "-", text)
    text = text.strip(" .-")
    return text[:80] or fallback


def _load_bundle(path: Path) -> Bundle:
    root = _expand_path(path).resolve()
    if root.is_file():
        root = root.parent
    meta_path = root / "metadata.json"
    transcript_path = root / "transcript.jsonl"
    if not meta_path.exists():
        raise ValueError(f"missing metadata.json in {root}")
    if not transcript_path.exists():
        raise ValueError(f"missing transcript.jsonl in {root}")
    metadata = _read_json(meta_path)
    raw = _read_jsonl(transcript_path)
    sid = str(metadata.get("session_id") or _session_id_from_transcript(raw) or root.name)
    title = str(metadata.get("title") or _title_from_session_json(root) or "")
    old_cwd = str(metadata.get("cwd") or "")
    started_at = str(metadata.get("started_at") or _timestamp_from_transcript(raw, first=True) or "")
    updated_at = str(metadata.get("updated_at") or _timestamp_from_transcript(raw, first=False) or started_at)
    return Bundle(root=root, metadata=metadata, transcript_path=transcript_path, raw=raw, session_id=sid, title=title, old_cwd=old_cwd, started_at=started_at, updated_at=updated_at)


def _session_id_from_transcript(raw: list[dict[str, Any]]) -> str:
    for obj in raw:
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        if obj.get("type") == "session_meta" and payload.get("id"):
            return str(payload["id"])
    return ""


def _timestamp_from_transcript(raw: list[dict[str, Any]], first: bool) -> str:
    found = ""
    for obj in raw:
        ts = str(obj.get("timestamp") or "")
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        if obj.get("type") == "session_meta" and payload.get("timestamp") and first:
            return str(payload["timestamp"])
        if ts and first:
            return ts
        if ts:
            found = ts
    return found


def _title_from_session_json(root: Path) -> str:
    path = root / "session.json"
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    meta = data.get("meta") if isinstance(data, dict) and isinstance(data.get("meta"), dict) else {}
    return str(meta.get("title") or "")


def _existing_session_ids(codex_home: Path) -> set[str]:
    ids: set[str] = set()
    index = codex_home / "session_index.jsonl"
    if index.exists():
        for obj in _read_index_rows(index):
            sid = str(obj.get("id") or "")
            if sid:
                ids.add(sid)
    sessions = codex_home / "sessions"
    if sessions.exists():
        for path in sessions.glob("*/*/*/*.jsonl"):
            m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", path.stem)
            if m:
                ids.add(m.group(1))
    return ids


def _read_index_rows(index: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not index.exists():
        return rows
    try:
        with index.open("r", encoding="utf-8") as f:
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
        return []
    return rows


def _new_session_id(existing: set[str]) -> str:
    while True:
        sid = str(uuid.uuid4())
        if sid not in existing:
            return sid


def _session_date(bundle: Bundle) -> datetime:
    original = str(bundle.metadata.get("transcript_path") or "")
    m = re.search(r"(?:^|[\\/])sessions[\\/](\d{4})[\\/](\d{2})[\\/](\d{2})(?:[\\/]|$)", original)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).astimezone()
    dt = _parse_dt(bundle.started_at) or _parse_dt(bundle.updated_at) or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()


def _transcript_dest(codex_home: Path, bundle: Bundle, session_id: str) -> Path:
    dt = _session_date(bundle)
    folder = codex_home / "sessions" / dt.strftime("%Y") / dt.strftime("%m") / dt.strftime("%d")
    original = str(bundle.metadata.get("transcript_path") or "")
    original_name = _basename_any_platform(original) if original else ""
    if original_name and bundle.session_id in original_name:
        name = original_name.replace(bundle.session_id, session_id)
    else:
        name = f"rollout-{dt.strftime('%Y-%m-%dT%H-%M-%S')}-{session_id}.jsonl"
    if not name.endswith(".jsonl"):
        name += ".jsonl"
    return folder / name


def _basename_any_platform(path_text: str) -> str:
    parts = [p for p in re.split(r"[\\/]+", path_text) if p]
    return parts[-1] if parts else ""


def _bundle_tree_files(root: Path, folder: str) -> list[Path]:
    base = root / folder
    if not base.exists() or not base.is_dir():
        return []
    return sorted(p for p in base.rglob("*") if p.is_file() and p.name != "_manifest.json")


def _copy_pairs(root: Path, workspace: Path, folder: str) -> list[tuple[Path, Path]]:
    pairs: list[tuple[Path, Path]] = []
    for src in _bundle_tree_files(root, folder):
        rel = src.relative_to(root)
        pairs.append((src, workspace / rel))
    return pairs


def _safe_rel_path(value: str) -> Optional[Path]:
    text = (value or "").replace("\\", "/").strip("/")
    if not text:
        return None
    parts = [p for p in text.split("/") if p and p != "."]
    if not parts or any(p == ".." for p in parts):
        return None
    return Path(*parts)


def _all_bundle_files(root: Path) -> list[Path]:
    skip_dirs = {"inputs", "outputs", "auth", ".git", "__pycache__"}
    out: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        parts = set(path.relative_to(root).parts)
        if parts & skip_dirs:
            continue
        out.append(path)
    return sorted(out)


def _auth_pairs(root: Path, codex_home: Path) -> list[tuple[Path, Path, str]]:
    auth_root = root / "auth"
    if not auth_root.exists() or not auth_root.is_dir():
        return []
    pairs: list[tuple[Path, Path, str]] = []
    seen: set[str] = set()
    manifest = _load_manifest(root, "auth")
    for item in manifest:
        if not isinstance(item, dict):
            continue
        bundle_path = str(item.get("bundle_path") or "")
        rel_text = str(item.get("relative_path") or "")
        if not bundle_path:
            continue
        rel = _safe_rel_path(rel_text)
        if rel is None:
            continue
        src_rel = _safe_rel_path(bundle_path)
        if src_rel is None:
            continue
        src = root / src_rel
        if not src.exists() or not src.is_file():
            continue
        rel_key = "/".join(rel.parts)
        if rel_key in seen:
            continue
        seen.add(rel_key)
        pairs.append((src, codex_home / rel, rel_key))
    if pairs:
        return pairs
    for src in sorted(p for p in auth_root.rglob("*") if p.is_file() and p.name not in {"_manifest.json", "_origin.json", "README.txt"}):
        rel = src.relative_to(auth_root)
        rel_key = "/".join(rel.parts)
        pairs.append((src, codex_home / rel, rel_key))
    return pairs


def _load_manifest(root: Path, folder: str) -> list[dict[str, Any]]:
    path = root / folder / "_manifest.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _load_auth_origin(root: Path) -> dict[str, Any]:
    path = root / "auth" / "_origin.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _path_variants(path_text: str) -> list[str]:
    if not path_text:
        return []
    variants = [path_text]
    file_path = _path_from_file_uri(path_text)
    if file_path:
        variants.append(file_path)
    for item in list(variants):
        variants.append(item.replace("\\", "/"))
        if not item.lower().startswith("file:"):
            variants.append(item.replace("/", "\\"))
    path_for_uri = file_path or path_text
    if re.match(r"^[A-Za-z]:[/\\]", path_for_uri):
        variants.append("file:///" + path_for_uri.replace("\\", "/"))
    elif path_for_uri.startswith("/"):
        variants.append("file://" + path_for_uri)
    out: list[str] = []
    seen: set[str] = set()
    for item in variants:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _path_from_file_uri(path_text: str) -> str:
    if not path_text.lower().startswith("file:"):
        return ""
    parsed = urlparse(path_text)
    path = unquote(parsed.path or "")
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        path = f"//{parsed.netloc}{path}"
    if re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    if sys.platform == "win32":
        path = path.replace("/", "\\")
    return path


def _add_replacement(replacements: dict[str, str], old: Any, new: Path | str) -> None:
    if not old:
        return
    new_text = str(new)
    for variant in _path_variants(str(old)):
        if variant and variant != new_text:
            replacements[variant] = new_text


def _build_replacements(bundle: Bundle, workspace: Path, session_id: str, input_pairs: list[tuple[Path, Path]], output_pairs: list[tuple[Path, Path]]) -> dict[str, str]:
    replacements: dict[str, str] = {}
    if bundle.old_cwd:
        _add_replacement(replacements, bundle.old_cwd, workspace)
    if session_id != bundle.session_id:
        replacements[bundle.session_id] = session_id

    copied_by_rel: dict[str, Path] = {}
    for _src, dest in input_pairs + output_pairs:
        copied_by_rel["/".join(dest.relative_to(workspace).parts)] = dest

    for folder in ("inputs", "outputs"):
        for item in _load_manifest(bundle.root, folder):
            if not isinstance(item, dict):
                continue
            bundle_path = str(item.get("bundle_path") or "")
            dest = copied_by_rel.get(bundle_path)
            if not dest:
                rel = str(item.get("relative_path") or "")
                if rel:
                    dest = copied_by_rel.get(f"{folder}/{rel}")
            if not dest:
                continue
            _add_replacement(replacements, item.get("source"), dest)
            _add_replacement(replacements, item.get("absolute_path"), dest)

    _add_basename_replacements(bundle.raw, replacements, input_pairs + output_pairs)
    return replacements


def _add_basename_replacements(raw: list[dict[str, Any]], replacements: dict[str, str], pairs: list[tuple[Path, Path]]) -> None:
    by_name: dict[str, Path] = {}
    duplicate: set[str] = set()
    for _src, dest in pairs:
        name = dest.name
        if name in by_name:
            duplicate.add(name)
        else:
            by_name[name] = dest
    for name in duplicate:
        by_name.pop(name, None)
    if not by_name:
        return
    text = "\n".join(json.dumps(obj, ensure_ascii=False) for obj in raw[:200])
    path_patterns = [
        r"(?<![\w.-])/[^\s`'\"<>|]+",
        r"(?<![\w.-])[A-Za-z]:[\\/][^\s`'\"<>|]+",
    ]
    for pattern in path_patterns:
        for match in re.finditer(pattern, text):
            old = match.group(0).rstrip(".,;:)]}")
            dest = by_name.get(_basename_any_platform(old))
            if dest:
                _add_replacement(replacements, old, dest)


def _rewrite_value(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        out = value
        for old, new in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
            out = out.replace(old, new)
        return out
    if isinstance(value, list):
        return [_rewrite_value(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _rewrite_value(item, replacements) for key, item in value.items()}
    return value


def _rewrite_transcript(bundle: Bundle, plan: ImportPlan, rewrite_paths: bool) -> list[dict[str, Any]]:
    replacements = plan.replacements if rewrite_paths else {bundle.session_id: plan.session_id}
    rows: list[dict[str, Any]] = []
    for obj in bundle.raw:
        row = _rewrite_value(obj, replacements)
        if not isinstance(row, dict):
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else None
        if row.get("type") == "session_meta" and payload is not None:
            payload["id"] = plan.session_id
            payload["cwd"] = str(plan.workspace)
        elif row.get("type") == "turn_context" and payload is not None:
            payload["cwd"] = str(plan.workspace)
        rows.append(row)
    return rows


def _copy_file_pairs(pairs: list[tuple[Path, Path]], dry_run: bool) -> int:
    copied = 0
    for src, dest in pairs:
        if not src.exists() or not src.is_file():
            continue
        try:
            if src.resolve(strict=False) == dest.resolve(strict=False):
                continue
        except OSError:
            pass
        copied += 1
        if dry_run:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    return copied


def _copy_auth_pairs(pairs: list[tuple[Path, Path, str]], *, dry_run: bool, force_auth: bool) -> int:
    copied = 0
    for src, dest, rel in pairs:
        if not src.exists() or not src.is_file():
            continue
        if dest.exists() and not force_auth:
            raise ValueError(f"auth file already exists at destination: {rel}; use --force-auth to overwrite")
        copied += 1
        if dry_run:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            backup = dest.with_suffix(dest.suffix + ".bak") if dest.suffix else dest.with_name(dest.name + ".bak")
            try:
                shutil.copy2(dest, backup)
            except OSError:
                pass
        shutil.copy2(src, dest)
    return copied


def _write_index(plan: ImportPlan, force: bool, dry_run: bool) -> bool:
    index = plan.index_path
    rows = _read_index_rows(index)
    next_row = {
        "id": plan.session_id,
        "thread_name": plan.bundle.display_title,
        "updated_at": plan.bundle.updated_at or plan.bundle.started_at or datetime.now(timezone.utc).isoformat(),
    }
    found = False
    out: list[dict[str, Any]] = []
    for row in rows:
        if row.get("id") == plan.session_id:
            found = True
            if force:
                out.append(next_row)
            else:
                out.append(row)
        else:
            out.append(row)
    if not found:
        out.append(next_row)
    if dry_run:
        return True
    index.parent.mkdir(parents=True, exist_ok=True)
    if index.exists():
        backup = index.with_suffix(index.suffix + ".bak")
        try:
            shutil.copy2(index, backup)
        except OSError:
            pass
    with index.open("w", encoding="utf-8", newline="\n") as f:
        for row in out:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    return True


def _write_workspace_docs(plan: ImportPlan, dry_run: bool) -> None:
    if dry_run:
        return
    plan.workspace.mkdir(parents=True, exist_ok=True)
    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    report: list[Optional[str]] = [
        f"# Restored Codex Session: {plan.bundle.display_title}",
        "",
        f"- Imported at: {now}",
        f"- Session ID: `{plan.session_id}`",
        f"- Original session ID: `{plan.bundle.session_id}`",
        f"- Original cwd: `{plan.bundle.old_cwd}`" if plan.bundle.old_cwd else None,
        f"- Workspace: `{plan.workspace}`",
        f"- Codex home: `{plan.codex_home}`",
        f"- Transcript: `{plan.transcript_dest}`",
        f"- Inputs copied: {plan.copied_inputs}",
        f"- Outputs copied: {plan.copied_outputs}",
        f"- Bundle files copied: {plan.copied_bundle_files}",
        f"- Auth snapshot files in bundle: {len(plan.auth_files)}",
        f"- Auth copied: {'yes' if plan.wrote_auth else 'no'}",
        "",
        "## Notes",
        "",
        "This restore does not copy Codex auth files, tokens, device ids, or account state unless auth restore is explicitly confirmed. The default no-auth mode expects the destination user's own logged-in Codex account.",
        "",
        "If the Codex sidebar does not show the imported conversation immediately, restart Codex so it rereads `session_index.jsonl` and the `sessions/` tree.",
        "",
    ]
    (plan.workspace / "RESTORE_REPORT.md").write_text(_join_doc_lines(report), encoding="utf-8")

    prompt: list[Optional[str]] = [
        f"# Continue Imported Codex Session: {plan.bundle.display_title}",
        "",
        "This workspace was restored from a codex-export bundle.",
        "",
        f"- Session ID: `{plan.session_id}`",
        f"- Restored workspace: `{plan.workspace}`",
        f"- Inputs: `inputs/`",
        f"- Outputs: `outputs/`",
        f"- Original export files: `original_bundle/`" if plan.bundle_files else None,
        "",
        "If the restored thread is visible in Codex, open it and continue normally. If not, start a new Codex chat in this workspace and use the exported `session.md` or `transcript.jsonl` under `original_bundle/` as the conversation history reference.",
        "",
    ]
    (plan.workspace / "CONTINUE_PROMPT.md").write_text(_join_doc_lines(prompt), encoding="utf-8")


def _join_doc_lines(lines: Iterable[Optional[str]]) -> str:
    return "\n".join(line for line in lines if line is not None) + "\n"


def _build_plan(args: argparse.Namespace, bundle_path: Path, workspace: Optional[Path] = None, allow_existing: bool = False) -> ImportPlan:
    bundle = _load_bundle(bundle_path)
    codex_home = _expand_path(args.codex_home).resolve()
    existing_ids = _existing_session_ids(codex_home)
    session_id = bundle.session_id
    changed_id = False
    if args.new_id:
        session_id = _new_session_id(existing_ids)
        changed_id = True
    elif session_id in existing_ids and not args.force and not allow_existing:
        raise ValueError(
            f"session {session_id} already exists in {codex_home}; use --force to replace index/transcript or --new-id to import as a copy"
        )

    if workspace is None:
        workspace = _workspace_for_bundle(bundle, _expand_path(args.workspace_root).resolve())
    workspace = workspace.resolve()
    input_pairs = _copy_pairs(bundle.root, workspace, "inputs")
    output_pairs = _copy_pairs(bundle.root, workspace, "outputs")
    auth_pairs = _auth_pairs(bundle.root, codex_home)
    bundle_pairs: list[tuple[Path, Path]] = []
    if args.copy_bundle:
        for src in _all_bundle_files(bundle.root):
            bundle_pairs.append((src, workspace / "original_bundle" / src.relative_to(bundle.root)))

    transcript_dest = _transcript_dest(codex_home, bundle, session_id)
    plan = ImportPlan(
        bundle=bundle,
        codex_home=codex_home,
        workspace=workspace,
        session_id=session_id,
        transcript_dest=transcript_dest,
        index_path=codex_home / "session_index.jsonl",
        input_files=input_pairs,
        output_files=output_pairs,
        bundle_files=bundle_pairs,
        auth_files=auth_pairs,
        changed_id=changed_id,
    )
    plan.replacements = _build_replacements(bundle, workspace, session_id, input_pairs, output_pairs)
    return plan


def _workspace_for_bundle(bundle: Bundle, workspace_root: Path) -> Path:
    name = f"{_safe_name(bundle.display_title)}-{bundle.session_id[:8]}"
    return workspace_root / name


def _execute_plan(
    plan: ImportPlan,
    *,
    dry_run: bool,
    force: bool,
    rewrite_paths: bool,
    no_index: bool,
    include_auth: bool,
    force_auth: bool,
) -> ImportPlan:
    if plan.transcript_dest.exists() and not force and not dry_run:
        raise ValueError(f"transcript already exists: {plan.transcript_dest}; use --force or --new-id")
    rows = _rewrite_transcript(plan.bundle, plan, rewrite_paths)
    plan.copied_inputs = _copy_file_pairs(plan.input_files, dry_run)
    plan.copied_outputs = _copy_file_pairs(plan.output_files, dry_run)
    plan.copied_bundle_files = _copy_file_pairs(plan.bundle_files, dry_run)
    if include_auth:
        print(f"warning: {AUTH_RISK_WARNING}", file=sys.stderr)
        plan.copied_auth = _copy_auth_pairs(plan.auth_files, dry_run=dry_run, force_auth=force_auth)
        plan.wrote_auth = True
    if not dry_run:
        _write_jsonl(plan.transcript_dest, rows)
    plan.wrote_transcript = True
    if not no_index:
        plan.wrote_index = _write_index(plan, force=force, dry_run=dry_run)
    _write_workspace_docs(plan, dry_run)
    plan.wrote_workspace = True
    return plan


def _print_plan(plan: ImportPlan, dry_run: bool = False) -> None:
    mode = "dry run" if dry_run else "import"
    changed = " (new id)" if plan.changed_id else ""
    print(f"{mode}: {plan.bundle.display_title}")
    print(f"  session: {plan.bundle.session_id} -> {plan.session_id}{changed}")
    print(f"  workspace: {plan.workspace}")
    print(f"  codex home: {plan.codex_home}")
    print(f"  transcript: {plan.transcript_dest}")
    print(f"  inputs: {len(plan.input_files)}")
    print(f"  outputs: {len(plan.output_files)}")
    print(f"  copied bundle files: {len(plan.bundle_files)}")
    print(f"  auth snapshot files: {len(plan.auth_files)}")
    if plan.wrote_auth or plan.copied_auth:
        print(f"  auth copied: {plan.copied_auth}")
    print(f"  path rewrites: {len(plan.replacements)}")


def _print_auth_source(plan: ImportPlan) -> None:
    origin = _load_auth_origin(plan.bundle.root)
    print("", file=sys.stderr)
    print("This bundle contains an auth/ snapshot.", file=sys.stderr)
    print(f"Session: {plan.bundle.display_title} ({plan.bundle.session_id})", file=sys.stderr)
    if origin:
        exported_at = origin.get("exported_at") or "unknown time"
        host = origin.get("hostname") or "unknown host"
        user = origin.get("username") or "unknown user"
        platform = origin.get("platform") or "unknown platform"
        source_home = origin.get("source_codex_home") or "unknown Codex home"
        print(f"Exported from: {host} / {user} / {platform}", file=sys.stderr)
        print(f"Source Codex home: {source_home}", file=sys.stderr)
        print(f"Exported at: {exported_at}", file=sys.stderr)
    print(f"Destination Codex home: {plan.codex_home}", file=sys.stderr)
    print("Auth files in bundle:", file=sys.stderr)
    for _src, _dest, rel in plan.auth_files:
        print(f"  - {rel}", file=sys.stderr)


def confirm_auth_import_default(plan: ImportPlan) -> bool:
    _print_auth_source(plan)
    print("", file=sys.stderr)
    print("Restoring auth will write credentials into the destination Codex home.", file=sys.stderr)
    print("Only answer yes if this is your own bundle and the destination should use the same Codex account.", file=sys.stderr)
    print("Restore auth from this bundle? [y/N] ", end="", file=sys.stderr, flush=True)
    answer = sys.stdin.readline()
    return answer.strip().lower() in {"y", "yes"}


def confirm_auth_import_explicit(plan: ImportPlan) -> bool:
    _print_auth_source(plan)
    print("", file=sys.stderr)
    print(AUTH_RISK_WARNING, file=sys.stderr)
    print("Do not continue if this bundle came from someone else or from an untrusted location.", file=sys.stderr)
    print(f'Type "{AUTH_CONFIRM_PHRASE}" to restore auth: ', end="", file=sys.stderr, flush=True)
    answer = sys.stdin.readline()
    return answer.strip() == AUTH_CONFIRM_PHRASE


def resolve_auth_import_choice(args: argparse.Namespace, plan: ImportPlan) -> bool:
    if args.yes_i_know_this_is_risky and not args.include_auth:
        raise ValueError("--yes-i-know-this-is-risky is only valid with --include-auth")
    if not plan.auth_files:
        return False
    if args.skip_auth and args.include_auth:
        raise ValueError("choose either --skip-auth or --include-auth, not both")
    if args.skip_auth:
        return False
    if args.include_auth:
        if args.yes_i_know_this_is_risky:
            return True
        if confirm_auth_import_explicit(plan):
            return True
        raise ValueError("auth import cancelled; rerun with --include-auth --yes-i-know-this-is-risky for non-interactive automation")
    return confirm_auth_import_default(plan)


def _bundle_dirs(root: Path) -> list[Path]:
    root = _expand_path(root).resolve()
    if (root / "metadata.json").exists() and (root / "transcript.jsonl").exists():
        return [root]
    bundles = []
    if root.exists():
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child / "metadata.json").exists() and (child / "transcript.jsonl").exists():
                bundles.append(child)
    return bundles


def cmd_inspect(args: argparse.Namespace) -> int:
    try:
        for bundle_dir in _bundle_dirs(args.bundle):
            plan = _build_plan(args, bundle_dir, allow_existing=True)
            _print_plan(plan, dry_run=True)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    try:
        bundle = _load_bundle(args.bundle)
        workspace = _expand_path(args.workspace).resolve() if args.workspace else None
        plan = _build_plan(args, bundle.root, workspace)
        _execute_plan(
            plan,
            dry_run=args.dry_run,
            force=args.force,
            rewrite_paths=not args.no_rewrite,
            no_index=args.no_index,
            include_auth=resolve_auth_import_choice(args, plan),
            force_auth=args.force_auth,
        )
        _print_plan(plan, dry_run=args.dry_run)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_import_all(args: argparse.Namespace) -> int:
    bundles = _bundle_dirs(args.bundles_root)
    if not bundles:
        print(f"error: no codex-export bundles found under {args.bundles_root}", file=sys.stderr)
        return 1
    ok = 0
    for bundle_dir in bundles:
        try:
            plan = _build_plan(args, bundle_dir)
            _execute_plan(
                plan,
                dry_run=args.dry_run,
                force=args.force,
                rewrite_paths=not args.no_rewrite,
                no_index=args.no_index,
                include_auth=resolve_auth_import_choice(args, plan),
                force_auth=args.force_auth,
            )
            _print_plan(plan, dry_run=args.dry_run)
            ok += 1
        except ValueError as exc:
            print(f"warn: skipped {bundle_dir}: {exc}", file=sys.stderr)
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="codex-import",
        description="Restore codex-export bundles into a Codex home and workspace.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              codex-import inspect ./exports/<session-id>
              codex-import import ./exports/<session-id>
              codex-import import ./exports/<session-id> --workspace ~/Documents/RestoredProject
              codex-import import ./exports/<session-id> --new-id
              codex-import import-all ./exports --dry-run
              codex-import import ./exports/<session-id> --skip-auth
              codex-import import ./exports/<session-id> --include-auth --yes-i-know-this-is-risky

            Safety:
              Auth files are never restored without confirmation. Use auth
              import only for same-account cross-device migration and protect
              the bundle like a password.
        """),
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--codex-home",
        default=str(CODEX_HOME),
        metavar="DIR",
        help=f"Codex home directory to write into (default: {CODEX_HOME})",
    )
    common.add_argument(
        "--workspace-root",
        default=str(DEFAULT_WORKSPACE_ROOT),
        metavar="DIR",
        help=f"default parent for restored workspaces (default: {DEFAULT_WORKSPACE_ROOT})",
    )
    common.add_argument("--dry-run", action="store_true", help="show the import plan without writing files")
    common.add_argument("--new-id", action="store_true", help="import as a new session id instead of preserving the exported id")
    common.add_argument("--force", action="store_true", help="overwrite an existing imported transcript/index entry for the same session id")
    common.add_argument("--no-index", action="store_true", help="write the transcript but do not update session_index.jsonl")
    common.add_argument("--no-rewrite", action="store_true", help="do not rewrite old file paths/cwd inside the transcript")
    common.add_argument(
        "--include-auth",
        action="store_true",
        help="HIGH RISK: request restoring auth/ files from the bundle into <codex-home>",
    )
    common.add_argument(
        "--yes-i-know-this-is-risky",
        dest="yes_i_know_this_is_risky",
        action="store_true",
        help="skip the interactive auth warning for automation; valid only with --include-auth",
    )
    common.add_argument(
        "--i-understand-auth-risk",
        dest="yes_i_know_this_is_risky",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    common.add_argument(
        "--skip-auth",
        action="store_true",
        help="ignore auth/ in the bundle and restore only transcript/files",
    )
    common.add_argument(
        "--force-auth",
        action="store_true",
        help="allow --include-auth to overwrite existing destination auth files after writing .bak backups",
    )
    common.add_argument(
        "--no-copy-bundle",
        dest="copy_bundle",
        action="store_false",
        help="skip copying session.html/session.md/session.json/session.csv into workspace/original_bundle",
    )
    common.set_defaults(copy_bundle=True)

    sub = p.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("inspect", parents=[common], help="inspect one bundle or a directory of bundles")
    pi.add_argument("bundle", type=Path, help="bundle directory or parent directory")
    pi.set_defaults(func=cmd_inspect)

    pim = sub.add_parser("import", parents=[common], help="import one codex-export bundle")
    pim.add_argument("bundle", type=Path, help="bundle directory")
    pim.add_argument("--workspace", metavar="DIR", help="exact workspace directory to restore files into")
    pim.set_defaults(func=cmd_import)

    pia = sub.add_parser("import-all", parents=[common], help="import every bundle in a directory")
    pia.add_argument("bundles_root", type=Path, help="directory containing exported session folders")
    pia.set_defaults(func=cmd_import_all)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
