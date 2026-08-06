"""Markdown 草稿渲染器：只使用已验证的 claim。"""

from __future__ import annotations

import re
from typing import Any


def _chinese_char_count(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", text))


def render_report_markdown(report: dict, contract: dict) -> dict:
    claims = {c.get("claim_id"): c for c in (report.get("claims") or [])}
    sections = report.get("sections") or []
    data = contract.get("data_status") or {}
    rp = contract.get("report_period") or {}
    dc = report.get("data_context") or {}
    lines: list[str] = []
    add = lines.append

    add(report.get("title") or "")
    add("")
    add("【报告状态】")
    add(f"- 报告周期：{rp.get('period_start')} 至 {rp.get('period_end')}")
    add(f"- 生成模式：{report.get('generation_mode')}")
    add(f"- 事实截止日：{data.get('facts_cutoff') or '未披露'}")
    add(f"- 民调截止日：{data.get('poll_cutoff') or '未披露'}")
    add(f"- 当前快照：{dc.get('active_snapshot_id') or data.get('active_snapshot_id') or '未披露'}")
    add(f"- 覆盖版本：{dc.get('coverage_version') or data.get('coverage_version') or '未披露'}")
    add(f"- 数据缺口：{'、'.join(data.get('uncovered_date_range') or []) or '无'}")
    add("")

    for section in sections:
        add(section.get("heading") or "")
        add("")
        for cid in section.get("claim_ids") or []:
            claim = claims.get(cid)
            if not claim:
                continue
            add(claim.get("claim_text") or "")
            add("")

    add("【证据映射】")
    for claim in report.get("claims") or []:
        refs = []
        if claim.get("supporting_event_ids"):
            refs.append("event:" + ",".join(claim["supporting_event_ids"]))
        if claim.get("supporting_poll_ids"):
            refs.append("poll:" + ",".join(claim["supporting_poll_ids"]))
        if claim.get("supporting_source_ids"):
            refs.append("source:" + ",".join(claim["supporting_source_ids"]))
        if claim.get("supporting_gap_ids"):
            refs.append("gap:" + ",".join(claim["supporting_gap_ids"]))
        if claim.get("supporting_snapshot_dimensions"):
            refs.append("dim:" + ",".join(claim["supporting_snapshot_dimensions"]))
        add(f"- {claim.get('claim_id')} [{claim.get('claim_type')}] -> {'; '.join(refs) if refs else '无'}")

    text = "\n".join(lines)
    chinese_chars = _chinese_char_count(text)
    length_below = chinese_chars < 1800
    return {
        "markdown": text + "\n",
        "chinese_char_count": chinese_chars,
        "length_below_target": length_below,
    }
