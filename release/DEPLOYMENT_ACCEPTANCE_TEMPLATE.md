# 部署验收记录模板

发布候选：`tainan-assessment-offline-rc1`

> 由部署执行人员在真实部署电脑上填写；Mock 结果不得作为生产验收依据。

## 基本信息

| 项目 | 内容 |
| --- | --- |
| 部署电脑名称 |  |
| 部署日期 |  |
| 部署人员 |  |
| Python 版本 |  |
| 项目版本 |  |
| DeepSeek 模型 |  |
| 飞书交付模式 |  |

## 验收项

| 验收项 | 结果 |
| --- | --- |
| DeepSeek 凭据配置 | 通过 / 失败 |
| 飞书凭据轮换 | 通过 / 失败 |
| 安全审计 | 通过 / 失败 |
| Development preflight | 通过 / 失败 |
| Dry-run preflight | 通过 / 失败 |
| Live DeepSeek test | 通过 / 失败 |
| 飞书测试 | 通过 / 失败 |
| Production preflight | 通过 / 失败 |
| 手工 production 运行 | 通过 / 失败 |
| Word 生成 | 通过 / 失败 |
| 飞书交付 | 通过 / 失败 |
| 9 日任务安装 | 通过 / 失败 |
| 22 日任务安装 | 通过 / 失败 |

## 最终状态

```text
production_llm_ready：
production_delivery_ready：
production_pipeline_ready：
```

## 遗留问题

```text
（无 / 具体问题描述）
```

## 最终验收结论

```text
（验收通过 / 验收不通过；原因）
```

## 验收证据

- `pipeline_manifest.json`
- `delivery_receipt.json`
- `artifact_validation.json`
- `deepseek_production_preflight.json`
- 飞书群消息截图或消息 ID
