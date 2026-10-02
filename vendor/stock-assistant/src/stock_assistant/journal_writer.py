from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .approvals import ApprovalStore
from .reports import JournalDraft


class JournalConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class JournalWriteResult:
    status: str
    path: Path
    content_sha256: str


class ApprovedJournalWriter:
    ALLOWED_PREFIX = Path("wiki") / "20_Areas" / "Investments"

    def __init__(self, wiki_root: Path, approvals: ApprovalStore) -> None:
        self.wiki_root = wiki_root.resolve()
        self.approvals = approvals

    def _resolve_target(self, relative_path: str) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("journal path must be relative and cannot traverse")
        if relative.parts[: len(self.ALLOWED_PREFIX.parts)] != self.ALLOWED_PREFIX.parts:
            raise ValueError("journal path is outside the approved investment-journal directory")
        target = (self.wiki_root / relative).resolve()
        try:
            target.relative_to(self.wiki_root)
        except ValueError as exc:
            raise ValueError("journal target escapes WIKI root") from exc
        if target.suffix.casefold() != ".md":
            raise ValueError("journal target must be Markdown")
        return target

    def write(self, draft: JournalDraft, *, approval_id: str, user_id: str) -> JournalWriteResult:
        target = self._resolve_target(draft.relative_path)
        actual_hash = hashlib.sha256(draft.markdown.encode("utf-8")).hexdigest()
        if actual_hash != draft.payload.get("content_sha256"):
            raise ValueError("journal draft content hash mismatch")
        self.approvals.consume(
            approval_id,
            purpose="wiki_journal",
            user_id=user_id,
            payload=draft.payload,
        )
        if target.exists():
            existing_hash = hashlib.sha256(target.read_bytes()).hexdigest()
            if existing_hash == actual_hash:
                return JournalWriteResult("matched", target, actual_hash)
            raise JournalConflictError("journal path already exists with different content")

        target.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(draft.markdown)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, target)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
        return JournalWriteResult("created", target, actual_hash)

