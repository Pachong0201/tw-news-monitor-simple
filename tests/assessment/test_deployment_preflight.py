import copy
import json
from datetime import date
from pathlib import Path

from app.assessment.deployment_preflight import (
    build_deployment_preflight,
    write_preflight_files,
)
from app.assessment.evidence_pack_builder import load_yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG = PROJECT_ROOT / "config" / "election_assessment.yaml"


def _config():
    return load_yaml(CONFIG)


class TestDeploymentPreflight:
    def test_development_ready(self):
        result = build_deployment_preflight(
            "development",
            config=_config(),
            root=PROJECT_ROOT,
        )
        assert result["preflight_ready"] is True
        assert result["development_ready"] is True
        assert result["errors"] == []

    def test_dry_run_ready(self):
        result = build_deployment_preflight(
            "dry_run",
            config=_config(),
            root=PROJECT_ROOT,
            as_of=date(2026, 8, 9),
        )
        assert result["preflight_ready"] is True
        assert result["dry_run_ready"] is True
        assert result["credentials_present"] is False

    def test_production_not_ready_current_env(self):
        result = build_deployment_preflight(
            "production",
            config=_config(),
            root=PROJECT_ROOT,
            as_of=date(2026, 8, 9),
        )
        assert result["preflight_ready"] is False
        assert result["production_ready"] is False
        joined = "；".join(result["errors"])
        assert "DEEPSEEK_API_KEY 未配置" in joined
        assert "live DeepSeek 预检未完成" in joined
        assert "历史周期事实覆盖不完整" in joined
        assert "飞书旧凭据尚未确认轮换" in joined
        assert result["production_llm_ready"] is False
        assert result["delivery_capability"]["configured_mode"] == "app_file_upload"
        assert result["delivery_preflight_ready"] is False

    def test_production_missing_pack_error(self):
        result = build_deployment_preflight(
            "production",
            config=_config(),
            root=PROJECT_ROOT,
            as_of=date(2026, 8, 22),
        )
        assert result["preflight_ready"] is False
        assert any("证据包不存在" in e for e in result["errors"])

    def test_delivery_disabled_removes_delivery_error(self):
        cfg = _config()
        cfg["delivery"]["enabled"] = False
        cfg["security"]["feishu_credentials_rotated_after_incident"] = True
        result = build_deployment_preflight(
            "production",
            config=cfg,
            root=PROJECT_ROOT,
            as_of=date(2026, 8, 9),
        )
        assert not any("FEISHU_WEBHOOK 未配置" in e for e in result["errors"])
        assert result["delivery_capability"]["configured_mode"] == "delivery_disabled"
        assert result["delivery_preflight_ready"] is True

    def test_rotation_not_acknowledged_blocks_production(self):
        result = build_deployment_preflight(
            "production",
            config=_config(),
            root=PROJECT_ROOT,
            as_of=date(2026, 8, 9),
        )
        assert result["preflight_ready"] is False
        assert any(
            "飞书旧凭据尚未确认轮换" in e for e in result["errors"]
        )

    def test_rotation_acknowledged_passes_security_gate(self):
        cfg = _config()
        cfg["security"]["feishu_credentials_rotated_after_incident"] = True
        cfg["security"]["feishu_rotation_acknowledged_at"] = "2026-08-05"
        cfg["delivery"]["mode"] = "webhook_summary"
        import os

        os.environ["FEISHU_WEBHOOK"] = "https://open.feishu.cn/open-apis/bot/v2/hook/test_rotation"
        try:
            result = build_deployment_preflight(
                "production",
                config=cfg,
                root=PROJECT_ROOT,
                as_of=date(2026, 8, 9),
            )
        finally:
            os.environ.pop("FEISHU_WEBHOOK", None)
        assert not any("飞书旧凭据尚未确认轮换" in e for e in result["errors"])
        assert not any("FEISHU_WEBHOOK" in e for e in result["errors"])
        # 仍因 LLM 凭据与事实覆盖不足而阻断
        assert result["preflight_ready"] is False

    def test_webhook_mode_capability(self):
        cfg = _config()
        cfg["security"]["feishu_credentials_rotated_after_incident"] = True
        cfg["delivery"]["mode"] = "webhook_summary"
        import os

        os.environ["FEISHU_WEBHOOK"] = "https://open.feishu.cn/open-apis/bot/v2/hook/test_w"
        try:
            result = build_deployment_preflight(
                "production",
                config=cfg,
                root=PROJECT_ROOT,
                as_of=date(2026, 8, 9),
            )
        finally:
            os.environ.pop("FEISHU_WEBHOOK", None)
        matrix = result["delivery_capability"]
        assert matrix["configured_mode"] == "webhook_summary"
        assert matrix["delivery_preflight_ready"] is True
        assert matrix["file_delivery_supported"] is False

    def test_app_mode_does_not_require_webhook(self):
        cfg = _config()
        cfg["security"]["feishu_credentials_rotated_after_incident"] = True
        cfg["delivery"]["mode"] = "app_file_upload"
        import os

        os.environ["FEISHU_APP_ID"] = "appid"
        os.environ["FEISHU_APP_SECRET"] = "appsecret"
        os.environ["FEISHU_CHAT_ID"] = "chat123"
        os.environ.pop("FEISHU_WEBHOOK", None)
        try:
            result = build_deployment_preflight(
                "production",
                config=cfg,
                root=PROJECT_ROOT,
                as_of=date(2026, 8, 9),
            )
        finally:
            os.environ.pop("FEISHU_APP_ID", None)
            os.environ.pop("FEISHU_APP_SECRET", None)
            os.environ.pop("FEISHU_CHAT_ID", None)
        matrix = result["delivery_capability"]
        assert matrix["configured_mode"] == "app_file_upload"
        assert matrix["delivery_preflight_ready"] is True
        assert matrix["webhook_summary_ready"] is False
        assert matrix["file_delivery_supported"] is True

    def test_write_preflight_files(self, tmp_path):
        results = {
            "development": build_deployment_preflight(
                "development", config=_config(), root=PROJECT_ROOT
            ),
            "dry_run": build_deployment_preflight(
                "dry_run", config=_config(), root=PROJECT_ROOT, as_of=date(2026, 8, 9)
            ),
            "production": build_deployment_preflight(
                "production", config=_config(), root=PROJECT_ROOT, as_of=date(2026, 8, 9)
            ),
        }
        written = write_preflight_files(tmp_path, results, target_root=tmp_path)
        for level in ("development", "dry_run", "production"):
            path = written[level]
            assert path.exists()
            data = json.loads(path.read_text(encoding="utf-8"))
            assert data["preflight_level"] == level

    def test_schedule_days_from_config(self):
        result = build_deployment_preflight(
            "development", config=_config(), root=PROJECT_ROOT
        )
        assert result["schedule_days"] == [9, 22]

    def test_word_dependencies_ready(self):
        result = build_deployment_preflight(
            "development", config=_config(), root=PROJECT_ROOT
        )
        assert result["word_dependencies_ready"] is True

    def test_unknown_level_fails(self):
        result = build_deployment_preflight(
            "bogus", config=_config(), root=PROJECT_ROOT
        )
        assert result["preflight_ready"] is False
        assert any("未知级别" in e for e in result["errors"])
