"""Explicit worksheet roles for transaction ingestion.

The response workbook can contain several historical tabs.  Role resolution
is intentionally exact (case/whitespace normalised) so a newly named or
unknown sheet cannot silently become a production current source.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _split(value: str | None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.strip() for item in str(value or "").split(",") if item.strip()))


def _key(value: str | None) -> str:
    return " ".join(str(value or "").strip().casefold().split())


@dataclass(frozen=True)
class SourceRoleConfig:
    current: tuple[str, ...] = ("TRANSACTIONS_CURRENT",)
    legacy: tuple[str, ...] = ("表單回覆 3", "Form Responses 1", "Form Responses 2", "Form V2")
    history: tuple[str, ...] = ("History", "歷史", "紀錄")

    @classmethod
    def from_environment(cls) -> "SourceRoleConfig":
        return cls(
            current=_split(os.getenv("CURRENT_TRANSACTION_SOURCE")) or cls.current,
            legacy=_split(os.getenv("LEGACY_TRANSACTION_SOURCES")) or cls.legacy,
            history=_split(os.getenv("HISTORY_SOURCE")) or cls.history,
        )

    def role_for(self, title: str) -> str | None:
        normalized = _key(title)
        if normalized in {_key(item) for item in self.current}:
            return "CURRENT"
        if normalized in {_key(item) for item in self.legacy}:
            return "LEGACY_ARCHIVE"
        if normalized in {_key(item) for item in self.history}:
            return "HISTORY"
        return None

    def as_dict(self) -> dict[str, list[str]]:
        return {"CURRENT": list(self.current), "LEGACY_ARCHIVE": list(self.legacy), "HISTORY": list(self.history)}


__all__ = ["SourceRoleConfig"]
