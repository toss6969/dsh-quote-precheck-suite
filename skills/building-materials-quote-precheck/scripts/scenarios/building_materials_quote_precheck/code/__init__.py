"""
building_materials_quote_precheck —— 建材报价预审:建材多供应商报价归一 + E 族判据 + 预审报告

定位(30_quote/00_构思方案_七插件.md §1 卡 4):
    P1 归一摄入(G-16: 文本PDF/XLSX/CSV → 统一记录;截图件 UNCERTAIN 转人工)
    → P2 判据链(Q11 横向异常 E-11 / Q13 单内一致 E-13 / Q14 MOQ包装 E-14 / Q15 完整度)
    → P3 预审报告(对照表+异常清单+UNCERTAIN 清单+VERDICT)
行业差异化:随包 industry_aliases.json(建材默认别名,steps.py 缺省自动加载)。
阈值治理:quote_rules_config.json 外置(threshold 缺/confirmed=false → UNKNOWN 记账,不硬判)。
"""

from __future__ import annotations

from scenario_api import InputField, InputKind, ScenarioDefinition, Step, StepPolicy, TaskDefinition

from .steps import step_p1_ingest, step_p2_evaluate, step_p3_report

SCENARIO_NAME = "building-materials-quote-precheck"
DISPLAY_NAME = "建材报价预审（Quote Precheck）"
VERSION = "0.1.0"


def build_scenario() -> ScenarioDefinition:
    # 界内/界外职责:SSOT = 仓根 INTENT.md §0/§1(NON_GOALS 六条,与母产品同源)。
    return ScenarioDefinition(
        name=SCENARIO_NAME,
        display_name=DISPLAY_NAME,
        version=VERSION,
        description=("对建材多供应商报价(文本PDF/XLSX/CSV)做清洗归一与横向拉平(全角数字/千分位自动归一),按 E 族判据产出预审报告;扫描件判 UNCERTAIN 转人工。"),
        tasks=(
            TaskDefinition(
                task_id="scan",
                label="建材报价预审",
                description="归一建材报价目录并跑 E 族判据,产出预审报告",
                input_fields=(
                    InputField(
                        field_id="input-dir",
                        label="报价文件目录",
                        kind=InputKind.TEXT,
                        default=".",
                        help_text="含报价文件(PDF/XLSX/CSV)的目录",
                    ),
                    InputField(
                        field_id="rules-config",
                        label="判据配置路径(可选)",
                        kind=InputKind.TEXT,
                        required=False,
                        default="quote_rules_config.json",
                        help_text="阈值外置配置;缺省用随包配置(CWD 优先,回落技能根)",
                    ),
                    InputField(
                        field_id="aliases-config",
                        label="列头别名扩展(可选)",
                        kind=InputKind.TEXT,
                        required=False,
                        default="",
                        help_text='外置别名扩展 JSON;不填时自动用随包 industry_aliases.json(只增不减)',
                    ),
                ),
                steps=(
                    Step(execute=step_p1_ingest, policy=StepPolicy(timeout_seconds=120),
                         name="p1_ingest"),
                    Step(execute=step_p2_evaluate, policy=StepPolicy(timeout_seconds=60),
                         name="p2_evaluate"),
                    step_p3_report,
                ),
            ),
        ),
    )
