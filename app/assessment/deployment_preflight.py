"""部署前置检查：development / dry_run / production 三级，不改生产预检结论。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

from .evidence_pack_builder import (
    db_business_hash,
    load_formal_data,
    load_yaml,
    read_only_conn,
)
from .reporting_period import PeriodError, resolve_reporting_period


REQUIRED_CODE_FILES = (
    "app/assessment/word_report_renderer.py",
    "app/assessment/report_artifact_validator.py",
    "app/assessment/run_assessment_pipeline.py",
    "app/assessment/deployment_preflight.py",
    "app/assessment/delivery/mock_delivery.py",
    "app/assessment/delivery/feishu_delivery.py",
    "app/assessment/delivery/delivery_factory.py",
    "app/assessment/pipeline_lock.py",
    "app/assessment/pipeline_manifest.py",
    "app/assessment/pipeline_state.py",
    "app/assessment/generate_llm_report.py",
    "app/assessment/build_evidence_pack.py",
)


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _check_word_deps(errors: list[str]) -> bool:
    try:
        import docx  # noqa: F401

        return True
    except Exception as exc:  # noqa: BLE001
        errors.append(f"word_dependencies: python-docx 不可用（{exc}）")
        return False


def _check_mock_provider(errors: list[str]) -> bool:
    try:
        from .llm import create_provider

        create_provider("mock")
        return True
    except Exception as exc:  # noqa: BLE001
        errors.append(f"mock_provider: 不可用（{exc}）")
        return False


def _check_mock_delivery(errors: list[str]) -> bool:
    try:
        from .delivery import create_delivery

        create_delivery("mock", mode="development")
        return True
    except Exception as exc:  # noqa: BLE001
        errors.append(f"mock_delivery: 不可用（{exc}）")
        return False


def _check_writable(root: Path, errors: list[str], warnings: list[str]) -> bool:
    candidates = [
        root / "data" / "reports" / "tainan_2026" / "deployment_validation",
        root / "data" / "reports" / "tainan_2026" / "pipeline_runs",
    ]
    ok = True
    for path in candidates:
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            ok = False
            errors.append(f"output_writable: {path.relative_to(root)} 不可写（{exc}）")
    return ok


def _check_formal_data(config: dict, root: Path, errors: list[str], warnings: list[str]) -> bool:
    try:
        formal = load_formal_data(config, root, config["election"]["election_id"])
    except Exception as exc:  # noqa: BLE001
        errors.append(f"formal_data: 正式数据读取失败（{exc}）")
        return False
    if formal.counts.get("formal_event_count", 0) <= 0:
        errors.append("formal_data: 正式事件数为 0")
    if formal.active_snapshot is None:
        errors.append("formal_data: 缺少 active snapshot")
    if not formal.coverage_dir.exists():
        errors.append(f"coverage: 覆盖目录不存在 {formal.coverage_dir}")
    return not errors


def _check_production_llm_preflight(root: Path, errors: list[str], warnings: list[str]) -> dict:
    path = (
        root
        / "data"
        / "reports"
        / "tainan_2026"
        / "deployment_validation"
        / "deepseek_production_preflight.json"
    )
    if not path.exists():
        errors.append("production_preflight: deepseek_production_preflight.json 不存在")
        return {}
    preflight = _json(path)
    if preflight.get("production_llm_ready") is not True:
        errors.append("production_preflight: production_llm_ready 未通过（live DeepSeek 预检未完成）")
    return preflight


def _check_delivery_preflight(config: dict, errors: list[str], warnings: list[str]) -> dict:
    """按三种交付模式检查生产交付门禁（含凭据轮换确认）。"""
    from .delivery.feishu_delivery import FeishuDelivery

    delivery = config.get("delivery") or {}
    enabled = bool(delivery.get("enabled", True))
    fallback = delivery.get("fallback_mode") or "none"
    security = config.get("security") or {}
    rotated = security.get("feishu_credentials_rotated_after_incident") is True
    try:
        probe = FeishuDelivery(config)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"delivery: 配置错误（{exc}）")
        return {
            "delivery_enabled": enabled,
            "configured_mode": "unknown",
            "webhook_summary_ready": False,
            "app_file_upload_ready": False,
            "file_delivery_supported": False,
            "delivery_preflight_ready": False,
            "missing_environment_variables": [],
        }
    matrix = probe.capability_matrix()
    if not enabled:
        warnings.append("delivery: 已显式关闭（delivery.enabled=false）")
    else:
        if fallback != "none":
            errors.append("delivery: fallback_mode 未实现，本轮禁止自动降级")
        if not matrix["delivery_preflight_ready"]:
            missing = "、".join(matrix["missing_environment_variables"]) or "交付凭据不完整"
            errors.append(f"delivery: {missing} 缺失（模式={matrix['configured_mode']}）")
    if not rotated:
        errors.append(
            "delivery: 飞书旧凭据尚未确认轮换（feishu_credentials_rotated_after_incident=false）"
        )
    matrix["delivery_preflight_ready"] = (
        matrix["delivery_preflight_ready"] and rotated
    )
    return matrix


def _check_model_valid(config: dict, errors: list[str], warnings: list[str]) -> tuple[str, str]:
    llm = config.get("llm", {}) or {}
    provider = llm.get("default_provider", "deepseek")
    ds = llm.get("deepseek", {}) or {}
    default_model = ds.get("default_model", "deepseek-v4-flash")
    model_env = ds.get("model_env", "DEEPSEEK_MODEL")
    allowed = set(ds.get("allowed_models") or [default_model])
    model = os.getenv(model_env) or default_model
    if provider != "deepseek":
        errors.append(f"default_provider: 生产要求 deepseek，当前 {provider}")
    if model in ("deepseek-chat", "deepseek-reasoner"):
        errors.append(f"model: 已弃用模型 {model}，请使用 deepseek-v4-flash 或 deepseek-v4-pro")
    elif model not in allowed:
        errors.append(f"model: {model} 不在允许列表 {sorted(allowed)}")
    return provider, model


def _check_period_coverage(
    config: dict,
    root: Path,
    errors: list[str],
    warnings: list[str],
    *,
    as_of: date | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
) -> None:
    try:
        tz_name = config.get("timezone", "Asia/Taipei")
        run_days = tuple(int(x) for x in (config.get("schedule", {}).get("run_days") or [9, 22]))
        raw_rules = (config.get("schedule", {}) or {}).get("periods") or {}
        period_rules = {
            int(key.split("_")[-1]): value
            for key, value in raw_rules.items()
            if key.startswith("day_") and value
        }
        period = resolve_reporting_period(
            timezone_name=tz_name,
            run_days=run_days,
            period_rules=period_rules or None,
            as_of=as_of,
            explicit_start=period_start,
            explicit_end=period_end,
        )
    except PeriodError as exc:
        errors.append(f"period: {exc}")
        return
    label = f"{period.period_start.isoformat()}_{period.period_end.isoformat()}"
    pack_path = (
        root
        / (config.get("paths") or {}).get(
            "output_root", "data/reports/tainan_2026/evidence_packages"
        )
        / label
        / "report_evidence_pack.json"
    )
    if not pack_path.exists():
        errors.append(
            f"period_coverage: 证据包不存在 {label}；历史周期事实覆盖不完整"
        )
        return
    pack = _json(pack_path)
    facts_cutoff = (pack.get("data_status") or {}).get("facts_cutoff")
    period_end_iso = period.period_end.isoformat()
    if not facts_cutoff or str(facts_cutoff) < period_end_iso:
        errors.append(
            f"period_coverage: facts_cutoff={facts_cutoff} 未覆盖 period_end={period_end_iso}；"
            "历史周期事实覆盖不完整"
        )


def build_deployment_preflight(
    level: str,
    *,
    config: dict,
    root: Path,
    as_of: date | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    if level not in ("development", "dry_run", "production"):
        errors.append(f"preflight_level: 未知级别 {level}")

    schedule = config.get("schedule", {}) or {}
    llm = config.get("llm", {}) or {}
    ds = llm.get("deepseek", {}) or {}
    default_provider = llm.get("default_provider", "deepseek")
    default_model = ds.get("default_model", "deepseek-v4-flash")
    credentials_present = bool(os.getenv("DEEPSEEK_API_KEY"))

    dev_ready = True
    if level in ("development", "dry_run", "production"):
        for rel in REQUIRED_CODE_FILES:
            if not (root / rel).exists():
                errors.append(f"code_file: 缺失 {rel}")
                dev_ready = False
        _check_word_deps(errors)
        _check_mock_provider(errors)
        _check_mock_delivery(errors)
        _check_writable(root, errors, warnings)

    dry_run_ready = dev_ready
    if level in ("dry_run", "production"):
        dry_run_ready = _check_formal_data(config, root, errors, warnings)

    production_ready = dry_run_ready
    live_deepseek_test = "not_run"
    if level == "production":
        production_ready = bool(os.getenv("DEEPSEEK_API_KEY"))
        if not credentials_present:
            errors.append("credentials: DEEPSEEK_API_KEY 未配置")
        _check_model_valid(config, errors, warnings)
        preflight = _check_production_llm_preflight(root, errors, warnings)
        live_deepseek_test = str(preflight.get("live_deepseek_test") or "not_run")
        delivery_matrix = _check_delivery_preflight(config, errors, warnings)
        if not sys.executable:
            errors.append("python_env: 无法定位 Python 解释器")
        _check_period_coverage(
            config,
            root,
            errors,
            warnings,
            as_of=as_of,
            period_start=period_start,
            period_end=period_end,
        )
        production_ready = production_ready and not errors

    ready = not errors
    preflight_ready = {
        "development": dev_ready and not errors,
        "dry_run": dry_run_ready and not errors,
        "production": production_ready and not errors,
    }.get(level, False)
    return {
        "preflight_level": level,
        "preflight_ready": preflight_ready,
        "errors": errors,
        "warnings": warnings,
        "development_ready": dev_ready and not errors if level == "development" else dev_ready,
        "dry_run_ready": dry_run_ready and not errors if level == "dry_run" else dry_run_ready,
        "production_ready": (
            production_ready and not errors if level == "production" else False
        ),
        "schedule_days": list(schedule.get("run_days") or [9, 22]),
        "default_provider": default_provider,
        "default_model": default_model,
        "credentials_present": credentials_present,
        "live_deepseek_test": live_deepseek_test,
        "production_llm_ready": False,
        "file_delivery_supported": (
            delivery_matrix.get("file_delivery_supported", False)
            if level == "production"
            else None
        ),
        "delivery_capability": (
            delivery_matrix if level == "production" else None
        ),
        "delivery_preflight_ready": (
            delivery_matrix.get("delivery_preflight_ready") if level == "production" else None
        ),
        "formal_data_readable": not any(
            e.startswith("formal_data") or e.startswith("coverage") for e in errors
        ),
        "word_dependencies_ready": _check_word_deps([]),
        "production_llm_ready_unchanged": True,
    }


def write_preflight_files(
    root: Path,
    results: dict[str, dict],
    target_root: Path | None = None,
) -> dict[str, Path]:
    base = target_root or root
    out_dir = base / "data" / "reports" / "tainan_2026" / "deployment_validation"
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for level, result in results.items():
        path = out_dir / f"deployment_preflight_{level}.json"
        path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        written[level] = path
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description="台南选情部署前置检查")
    parser.add_argument("--config", default="config/election_assessment.yaml")
    parser.add_argument("--level", choices=["development", "dry_run", "production"], default="development")
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--period-start", default=None)
    parser.add_argument("--period-end", default=None)
    parser.add_argument("--write-files", action="store_true")
    args = parser.parse_args()

    root = Path(args.config).resolve().parent.parent
    config = load_yaml(Path(args.config))

    def _d(value: str | None):
        return date.fromisoformat(value) if value else None

    result = build_deployment_preflight(
        args.level,
        config=config,
        root=root,
        as_of=_d(args.as_of),
        period_start=_d(args.period_start),
        period_end=_d(args.period_end),
    )
    if args.write_files:
        write_preflight_files(root, {args.level: result})
    print(f"preflight_level={args.level}")
    print(f"preflight_ready={result['preflight_ready']}")
    for e in result["errors"]:
        print(f"ERROR: {e}")
    for w in result["warnings"]:
        print(f"WARN: {w}")
    return 0 if result["preflight_ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
