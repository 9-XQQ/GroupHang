"""Normalize user-exported social text without fetching third-party pages."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
_EXPAND_REPLIES_RE = re.compile(r"^(?:展开|查看)(?:其余)?\s*\d+\s*条回复$")
_REACTION_ONLY_RE = re.compile(r"^(?:赞|点赞|收藏|回复)\s*\d*$")


@dataclass(frozen=True)
class SourceAdapter:
    platform: str
    display_name: str
    reply_labels: tuple[str, ...] = ()

    def normalize(self, text: str | None, comments: str | None) -> dict:
        return {
            "text": _normalize_block(text),
            "comments": _normalize_comments(comments, self.reply_labels),
            "adapter": f"{self.platform}_manual_export_v1",
            "display_name": self.display_name,
        }


def _normalize_block(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "")
    text = _ZERO_WIDTH_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _normalize_comments(value: str | None, reply_labels: tuple[str, ...]) -> str:
    lines = []
    for raw_line in _normalize_block(value).splitlines():
        line = raw_line.strip()
        if not line or _EXPAND_REPLIES_RE.fullmatch(line) or _REACTION_ONLY_RE.fullmatch(line):
            continue
        for label in reply_labels:
            if line.startswith(label):
                line = f"回复：{line[len(label):].lstrip(' :：')}"
                break
        lines.append(line)
    return "\n".join(lines)


_ADAPTERS = {
    "generic": SourceAdapter("generic", "普通文本"),
    "xiaohongshu": SourceAdapter("xiaohongshu", "小红书", ("作者回复", "楼主回复")),
    "dianping": SourceAdapter("dianping", "大众点评", ("商家回复", "作者回复")),
    "meituan": SourceAdapter("meituan", "美团", ("商家回复", "作者回复")),
    "other": SourceAdapter("other", "其他来源"),
}


def adapt_source_text(platform: str, text: str, comments: str | None) -> dict:
    """Return normalized text from a manual copy/export; this function never performs I/O."""
    adapter = _ADAPTERS.get(platform, _ADAPTERS["other"])
    result = adapter.normalize(text, comments)
    result["warnings"] = [] if platform == "generic" else [
        f"已按{adapter.display_name}手动导出格式清理文本；未连接或抓取平台页面"
    ]
    return result
