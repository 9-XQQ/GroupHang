"""Phase 3 分享文本的确定性地点提取；后续可在同一接口后接 LLM。"""
import re

_URL_RE = re.compile(r"https?://[^\s，。；;]+")
_LABELS = {
    "name": ("名称", "店名", "地点", "景点"),
    "address": ("地址", "位置"),
    "category": ("品类", "分类", "类型"),
    "price": ("人均", "价格"),
    "reason": ("推荐理由", "推荐", "理由"),
}


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
