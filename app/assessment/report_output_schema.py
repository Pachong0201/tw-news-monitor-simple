"""严格结构化报告输出 Schema 校验（不使用 jsonschema 依赖）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "tainan_assessment_report_v1.schema.json"

REPORT_REQUIRED = [
    "schema_version",
    "report_id",
    "election_id",
    "report_period",
    "generation_mode",
    "report_status",
    "title",
    "title_claim_ids",
    "overall_judgment_claim_ids",
    "sections",
    "claims",
    "required_disclosures",
    "do_not_infer_compliance",
    "report_statistics",
    "data_context",
]

SCHEMA_VERSION = "1.1"

DATA_CONTEXT_REQUIRED = [
    "active_snapshot_id",
    "previous_snapshot_id",
    "coverage_version",
    "facts_cutoff",
    "poll_cutoff",
    "period_start",
    "period_end",
    "uncovered_date_range",
]

CLAIM_REQUIRED = [
    "claim_id",
    "claim_type",
    "claim_text",
    "confidence",
    "material_for_report",
    "supporting_event_ids",
    "supporting_poll_ids",
    "supporting_source_ids",
    "supporting_snapshot_dimensions",
    "supporting_gap_ids",
    "inference_basis",
    "limitations",
    "applies_to_period",
]

CLAIM_TYPES = {
    "factual_synthesis",
    "current_assessment",
    "comparative_assessment",
    "forward_outlook",
    "limitation",
    "data_disclosure",
}
CONFIDENCE_LEVELS = {"high", "medium", "low", "not_applicable"}
GENERATION_MODES = {"final", "draft_with_data_gap"}
REPORT_STATUSES = {"generated", "repaired", "rejected"}


def load_schema() -> dict:
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        return json.load(f)


def validate_report_schema(report: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(report, dict):
        return ["输出不是 JSON 对象"]
    allowed_top = set(REPORT_REQUIRED) | {"report_statistics"}
    extra = set(report) - allowed_top
    if extra:
        errors.append(f"顶层多余字段: {sorted(extra)}")
    missing = [k for k in REPORT_REQUIRED if k not in report]
    if missing:
        errors.append(f"缺少顶层必需字段: {missing}")
    if report.get("schema_version") != SCHEMA_VERSION:
        errors.append(
            f"schema_version 必须为 {SCHEMA_VERSION}，实际 {report.get('schema_version')!r}"
        )
    if report.get("generation_mode") not in GENERATION_MODES:
        errors.append(f"generation_mode 非法: {report.get('generation_mode')!r}")
    if report.get("report_status") not in REPORT_STATUSES:
        errors.append(f"report_status 非法: {report.get('report_status')!r}")

    sections = report.get("sections") or []
    if not isinstance(sections, list):
        errors.append("sections 必须是数组")
    else:
        for i, section in enumerate(sections):
            if not isinstance(section, dict):
                errors.append(f"sections[{i}] 不是对象")
                continue
            for key in ("section_id", "heading", "claim_ids", "section_purpose"):
                if key not in section:
                    errors.append(f"sections[{i}] 缺少 {key}")
            if set(section) - {"section_id", "heading", "claim_ids", "section_purpose"}:
                errors.append(f"sections[{i}] 包含额外字段")

    claims = report.get("claims") or []
    if not isinstance(claims, list):
        errors.append("claims 必须是数组")
    else:
        for i, claim in enumerate(claims):
            if not isinstance(claim, dict):
                errors.append(f"claims[{i}] 不是对象")
                continue
            missing_claim = [k for k in CLAIM_REQUIRED if k not in claim]
            if missing_claim:
                errors.append(f"claims[{i}] 缺少字段: {missing_claim}")
            if set(claim) - set(CLAIM_REQUIRED):
                errors.append(f"claims[{i}] 包含额外字段")
            if claim.get("claim_type") not in CLAIM_TYPES:
                errors.append(f"claims[{i}] claim_type 非法: {claim.get('claim_type')!r}")
            if claim.get("confidence") not in CONFIDENCE_LEVELS:
                errors.append(f"claims[{i}] confidence 非法: {claim.get('confidence')!r}")
            if not isinstance(claim.get("claim_text"), str) or not claim.get("claim_text"):
                errors.append(f"claims[{i}] claim_text 为空")

    for key in ("title_claim_ids", "overall_judgment_claim_ids", "required_disclosures"):
        if not isinstance(report.get(key), list):
            errors.append(f"{key} 必须是数组")
    dni = report.get("do_not_infer_compliance") or []
    if not isinstance(dni, list):
        errors.append("do_not_infer_compliance 必须是数组")
    else:
        for i, item in enumerate(dni):
            if not isinstance(item, dict) or not {"rule_id", "rule_text", "violated", "related_claim_ids"} <= set(item):
                errors.append(f"do_not_infer_compliance[{i}] 结构非法")
    stats = report.get("report_statistics")
    if not isinstance(stats, dict):
        errors.append("report_statistics 必须是对象")
    data_context = report.get("data_context")
    if not isinstance(data_context, dict):
        errors.append("data_context 必须是对象")
    else:
        missing_dc = [k for k in DATA_CONTEXT_REQUIRED if k not in data_context]
        if missing_dc:
            errors.append(f"data_context 缺少字段: {missing_dc}")
        extra_dc = set(data_context) - set(DATA_CONTEXT_REQUIRED)
        if extra_dc:
            errors.append(f"data_context 包含额外字段: {sorted(extra_dc)}")
        for key in (
            "active_snapshot_id",
            "coverage_version",
            "facts_cutoff",
            "poll_cutoff",
            "period_start",
            "period_end",
        ):
            if key in data_context and not isinstance(data_context[key], str):
                errors.append(f"data_context.{key} 必须是字符串")
        if not isinstance(data_context.get("uncovered_date_range"), list):
            errors.append("data_context.uncovered_date_range 必须是数组")
    return errors


def is_valid_report_schema(report: dict) -> bool:
    return not validate_report_schema(report)
