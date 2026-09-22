"""Phase 3 分享文本的确定性地点提取；后续可在同一接口后接 LLM。"""
import re
from collections import Counter

_URL_RE = re.compile(r"https?://[^\s，。；;]+")
_LABELS = {
    "name": ("名称", "店名", "地点", "景点"),
    "address": ("地址", "位置"),
    "category": ("品类", "分类", "类型"),
    "price": ("人均", "价格"),
    "reason": ("推荐理由", "推荐", "理由"),
}

_COMMENT_TOPICS = {
    "口味": ("好吃", "难吃", "味道", "口味", "偏甜", "偏咸", "正宗"),
    "营业稳定性": ("营业", "开门", "关门", "时间不固定", "扑空"),
    "排队拥挤": ("排队", "等位", "拥挤", "人多"),
    "服务": ("服务", "态度", "店员"),
    "价格": ("价格", "贵", "便宜", "性价比", "人均"),
    "交通位置": ("交通", "地铁", "停车", "位置", "好找", "难找"),
}
_POSITIVE_WORDS = ("好吃", "推荐", "不错", "喜欢", "方便", "便宜", "值得", "正宗", "稳定")
_NEGATIVE_WORDS = ("难吃", "不推荐", "踩雷", "失望", "不便", "贵", "排队", "拥挤", "偏甜", "偏咸", "扑空", "不固定")


def _clean(value: str) -> str:
    return value.strip().strip("-—•*# ").strip()


def _label_value(line: str, labels: tuple[str, ...]) -> str | None:
    joined = "|".join(map(re.escape, labels))
    match = re.match(rf"^(?:{joined})\s*[：:]\s*(.+)$", line, re.I)
    return _clean(match.group(1)) if match else None


def parse_shared_text(text: str, default_stay_min: int = 60) -> dict:
    """提取最多 10 个候选地点，保留来源链接与无法确定字段的警告。"""
    source_urls = list(dict.fromkeys(_URL_RE.findall(text)))
    content = _URL_RE.sub("", text.replace("\r\n", "\n"))
    paragraphs = [block.strip() for block in re.split(r"\n\s*\n", content) if block.strip()]
    blocks = []
    for paragraph in paragraphs:
        lines = [line for line in paragraph.splitlines() if line.strip()]
        if len(lines) > 1 and all(re.search(r"[|｜\t]", line) for line in lines):
            blocks.extend(lines)
        else:
            blocks.append(paragraph)
    candidates: list[dict] = []

    for block in blocks:
        lines = [_clean(line) for line in block.splitlines() if _clean(line)]
        if not lines:
            continue
        # 一行一个地点：名称｜地址｜品类｜人均｜理由
        if len(lines) == 1 and re.search(r"[|｜\t]", lines[0]):
            parts = [_clean(part) for part in re.split(r"[|｜\t]", lines[0])]
            parts += [""] * (5 - len(parts))
            name, address, category, price, reason = parts[:5]
        else:
            values: dict[str, str] = {}
            unlabeled = []
            for line in lines:
                matched = False
                for field, labels in _LABELS.items():
                    value = _label_value(line, labels)
                    if value is not None:
                        values[field] = value
                        matched = True
                        break
                if not matched:
                    unlabeled.append(line)
            name = values.get("name") or (unlabeled[0] if unlabeled else "")
            address = values.get("address", "")
            category = values.get("category", "")
            price = values.get("price", "")
            reason = values.get("reason") or "；".join(unlabeled[1:])
        if not name:
            continue
        candidates.append({
            "name": name[:120], "address": address[:300], "category": category[:80],
            "price": price[:50], "reason": reason[:500], "expected_stay_min": default_stay_min,
            "source_url": source_urls[0] if source_urls else None,
        })
        if len(candidates) == 10:
            break

    warnings = []
    if not candidates:
        warnings.append("没有识别到地点，请使用“名称｜地址｜品类｜人均｜推荐理由”，每个地点之间空一行")
    if len(blocks) > 10:
        warnings.append("首版最多解析 10 个地点，其余内容已忽略")
    return {"candidates": candidates, "source_urls": source_urls, "warnings": warnings}


def summarize_comments(text: str | None) -> dict:
    """对用户主动粘贴的评论做可追溯计数；不抓取平台，也不把观点当事实。"""
    lines = [_clean(line) for line in (text or "").replace("\r\n", "\n").splitlines() if _clean(line)]
    threads: list[dict] = []
    current: dict | None = None
    for line in lines:
        comment_match = re.match(r"^(?:评论|主评论)\s*[：:]\s*(.+)$", line)
        reply_match = re.match(r"^(?:回复|楼主回复)\s*[：:]\s*(.+)$", line)
        if comment_match:
            current = {"comment": _clean(comment_match.group(1))[:300], "replies": []}
            threads.append(current)
        elif reply_match:
            reply = _clean(reply_match.group(1))[:300]
            if current is None:
                current = {"comment": "（未提供主评论）", "replies": []}
                threads.append(current)
            current["replies"].append(reply)
        else:
            current = {"comment": line[:300], "replies": []}
            threads.append(current)

    topics = []
    for topic, keywords in _COMMENT_TOPICS.items():
        matched = [line for line in lines if any(keyword in line for keyword in keywords)]
        if not matched:
            continue
        positive = sum(1 for line in matched if any(word in line for word in _POSITIVE_WORDS))
        negative = sum(1 for line in matched if any(word in line for word in _NEGATIVE_WORDS))
        mixed_or_neutral = sum(
            1 for line in matched
            if not any(word in line for word in _POSITIVE_WORDS)
            and not any(word in line for word in _NEGATIVE_WORDS)
        )
        topics.append({
            "topic": topic,
            "mentions": len(matched),
            "positive": positive,
            "negative": negative,
            "mixed_or_neutral": mixed_or_neutral,
            "samples": [line[:120] for line in matched[:3]],
        })
    for index, thread in enumerate(threads, start=1):
        thread["thread_id"] = f"thread-{index}"
        combined = [thread["comment"], *thread["replies"]]
        thread["reply_count"] = len(thread["replies"])
        thread["topics"] = [
            topic for topic, keywords in _COMMENT_TOPICS.items()
            if any(any(keyword in item for keyword in keywords) for item in combined)
        ]
        thread["replies"] = thread["replies"][:20]
    return {
        "comment_count": len(threads),
        "reply_count": sum(thread["reply_count"] for thread in threads),
        "topics": topics,
        "threads": threads[:50],
    }


def associate_comments_to_candidates(comment_summary: dict, candidates: list[dict]) -> list[dict]:
    """只在楼层明确写出地点名称时关联，避免根据“第一家”等指代进行猜测。"""
    threads = comment_summary.get("threads") or []
    associated = []
    for candidate in candidates:
        name = str(candidate.get("name") or "").strip()
        aliases = {name, re.sub(r"[（(].*?[）)]", "", name).strip()}
        aliases = {re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", alias.lower()) for alias in aliases}
        aliases = {alias for alias in aliases if len(alias) >= 2}
        matched = []
        for thread in threads:
            combined = " ".join([thread.get("comment", ""), *(thread.get("replies") or [])])
            normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", combined.lower())
            if aliases and any(alias in normalized for alias in aliases):
                matched.append(thread)
        if matched:
            topic_counts = Counter(topic for thread in matched for topic in (thread.get("topics") or []))
            candidate = {
                **candidate,
                "comment_insights": {
                    "matched_thread_count": len(matched),
                    "reply_count": sum(int(thread.get("reply_count") or 0) for thread in matched),
                    "topic_counts": dict(topic_counts.most_common()),
                    "threads": matched[:5],
                    "matching_rule": "explicit_place_name",
                },
            }
        associated.append(candidate)
    return associated
