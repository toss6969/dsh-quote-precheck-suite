---
name: fastener-quote-precheck
description: 对紧固件(螺栓/螺钉/螺母等)多供应商报价(文本PDF/XLSX/CSV)做清洗归一与横向拉平,按 E 族判据产出预审报告;扫描件判 UNCERTAIN 转人工。
---
# 紧固件报价预审（Quote Precheck）

> 本文件是技能的对外契约(指令式)。发布前三平台的 manifest/description 均以本文件为源。
> 依赖声明走 frontmatter dependency(官方位);不要另建 build/requirements.txt。

## 何时使用

用户给出**多个供应商的紧固件报价文件**(文本型 PDF / XLSX / CSV,常为混合格式),需要:
- 横向拉平对照(同一品项各供应商单价/数量/MOQ 一张表)
- 价格异常预审(横向价差 / 同单不一致 / MOQ·包装异常)
- 字段完整度与清洗留痕(表头识别、币种/千分位/全角/单位后缀归一)
- 扫描件/不可解析件明确转人工清单

比价决策归人:本技能产出预审报告(VERDICT),不替用户拍板选供应商。

## 执行步骤

1. 向用户确认报价文件所在目录(需文本层:扫描件/截图请用户先转文本层,残余不可解析件一律进 UNCERTAIN 转人工清单,不做 OCR)。
2. 运行本地确定性引擎。**路径基准:本 SKILL.md 所在目录 = 技能根**,命令相对该目录执行:

   ```bash
   cd <技能根 = 本 SKILL.md 所在目录>
   python ./scripts/engine/main.py run --scenario fastener-quote-precheck --json --task scan --set input-dir=<报价文件目录>
   ```

   可选参数:`--set rules-config=<判据配置路径>`(缺省用随包 `quote_rules_config.json`)、
   `--set aliases-config=<列头别名扩展 JSON>`(现场方言;不填时自动用随包 `industry_aliases.json`
   的紧固件默认别名,只增不减)。
   任务契约探测:`python ./scripts/engine/main.py tasks --scenario fastener-quote-precheck --json`
   环境自检(退出码 0=正常):`python ./scripts/engine/main.py selfcheck`
3. 读 stdout 严格 JSON:以 `state` / `artifacts` / `issues` 为准向用户转述。
   - `state=REVIEW_REQUIRED`:报告含异常 findings,交人工复核
   - `state=DONE`:机器面无异常
   - 产物 `report_file`(人读 Markdown 预审报告)+ `findings_file`(机器可读 JSON)
4. 宣称"已生成"前核对 artifacts 文件真实存在;报错原样转告,不猜不改输入。
5. 向用户转述报告四部分:横向对照表 / 清洗台账 / 异常清单 / UNCERTAIN 转人工清单 + 末行 VERDICT。

## 运行环境

- Python 3.10+;依赖见上方 frontmatter(首次使用先 pip install)。
- 推荐设置 `LOCAL_FLOW_DIST_ROOT=<可写目录>` 再运行,产物不落在插件目录内。
- 零写入报价源文件;单次运行无长期状态。

## 判据引用

本文件不复述判据值域与阈值,以下文件是唯一事实源:

- 域判据骨架(证据优先级 / 域裁决规则 / 紧固件行业口径 / 反模式):@references/EVALUATION_CRITERIA.md
- 判据阈值与治理态(threshold/confirmed/出处):随包 `quote_rules_config.json`(改口径=改配置不改代码)

## 红线

- 结论只认引擎报告;待人工项(冲突/不可比/未解析)如实转告用户。
- 引擎零 LLM 调用、零网络:宿主只整理输入与转述结论,不替引擎下判定。
- 不编造值;判定不了就转人工(UNCERTAIN),禁止对截图件/缺失字段硬判。
