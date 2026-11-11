# dsh-quote-precheck-suite

22 个行业报价预审技能：多供应商报价（文本PDF/XLSX/CSV）清洗归一、横向拉平比对、价格异常预审；单位口径不可比、扫描件或不可解析件显式转人工，产出预审报告，不替用户拍板选供应商。

适配 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（一切皆插件）。
22 个技能均为「确定性引擎 + 指令契约」：判定逻辑落在 Python 引擎里，结果可复算，
对不上的项点名到行；读不出或单位口径不可比的显式转人工，不靠模型现算，
**只产出预审报告（VERDICT），不替用户拍板选供应商。**

## 技能清单（22）

| 技能 | 说明 |
|---|---|
| `auto-parts-quote-precheck` | 对汽车零部件多供应商报价(文本PDF/XLSX/CSV)做清洗归一与横向拉平,按 E 族判据产出预审报告;零件号/OE号方言优先识别;扫描件判 UNCERTAIN 转人工。 |
| `building-materials-quote-precheck` | 对建材多供应商报价(文本PDF/XLSX/CSV)做清洗归一与横向拉平(全角数字/千分位自动归一),按 E 族判据产出预审报告;扫描件判 UNCERTAIN 转人工。 |
| `chemical-quote-precheck` | 对化工原料多供应商报价(文本PDF/XLSX/CSV)做清洗归一与横向拉平(万元/千分位/全角自动归一),按 E 族判据产出预审报告;扫描件判 UNCERTAIN 转人工。 |
| `cosmetics-quote-precheck` | 对日化美妆产品多供应商报价(文本PDF/XLSX/CSV)做清洗归一与横向拉平,按 E 族判据产出预审报告;扫描件判 UNCERTAIN 转人工。 |
| `electronic-components-quote-precheck` | 对电子元器件多供应商报价(文本PDF/XLSX/CSV)做清洗归一与横向拉平,按 E 族判据产出预审报告;料号/MPN/最小订购量方言与中英双语表头优先识别;扫描件判 UNCERTAIN 转人工。 |
| `equipment-maintenance-quote-precheck` | 对设备维保与备件多供应商报价(文本PDF/XLSX/CSV)做清洗归一与横向拉平,按 E 族判据产出预审报告;扫描件判 UNCERTAIN 转人工。 |
| `fastener-quote-precheck` | 对紧固件(螺栓/螺钉/螺母等)多供应商报价(文本PDF/XLSX/CSV)做清洗归一与横向拉平,按 E 族判据产出预审报告;扫描件判 UNCERTAIN 转人工。 |
| `food-beverage-quote-precheck` | 对食品饮料多供应商报价(文本PDF/XLSX/CSV)做清洗归一与横向拉平,按 E 族判据产出预审报告;扫描件判 UNCERTAIN 转人工。 |
| `fresh-frozen-quote-precheck` | 对生鲜冻品多供应商报价(文本PDF/XLSX/CSV)做清洗归一与横向拉平,按 E 族判据产出预审报告;扫描件判 UNCERTAIN 转人工。 |
| `home-goods-quote-precheck` | 对家居日用品多供应商报价(文本PDF/XLSX/CSV)做清洗归一与横向拉平,按 E 族判据产出预审报告;扫描件判 UNCERTAIN 转人工。 |
| `logistics-quote-precheck` | 对多家物流供应商报价(文本PDF/XLSX/CSV)做清洗归一与横向拉平,按 E 族判据产出预审报告;线路/服务项目方言优先识别;扫描件判 UNCERTAIN 转人工。 |
| `manufacturing-quote-precheck` | 对制造业零部件多供应商报价(文本PDF/XLSX/CSV)做清洗归一与横向拉平(万元倍率/币种符号自动归一),按 E 族判据产出预审报告;扫描件判 UNCERTAIN 转人工。 |
| `medical-consumables-quote-precheck` | 对医用耗材多供应商报价(文本PDF/XLSX/CSV)做清洗归一与横向拉平,按 E 族判据产出预审报告;扫描件判 UNCERTAIN 转人工。 |
| `office-supplies-quote-precheck` | 对办公用品多供应商报价(文本PDF/XLSX/CSV)做清洗归一与横向拉平,按 E 族判据产出预审报告;扫描件判 UNCERTAIN 转人工。 |
| `packaging-printing-quote-precheck` | 对包装印刷品多供应商报价(文本PDF/XLSX/CSV)做清洗归一与横向拉平,按 E 族判据产出预审报告;扫描件判 UNCERTAIN 转人工。 |
| `plastic-resin-quote-precheck` | 对塑胶原料多供应商报价(文本PDF/XLSX/CSV)做清洗归一与横向拉平,按 E 族判据产出预审报告;扫描件判 UNCERTAIN 转人工。 |
| `retail-quote-precheck` | 对零售商品多供应商报价(文本PDF/XLSX/CSV)做清洗归一与横向拉平,按 E 族判据产出预审报告;扫描件判 UNCERTAIN 转人工。 |
| `small-appliance-quote-precheck` | 对小家电多供应商报价(文本PDF/XLSX/CSV)做清洗归一与横向拉平,按 E 族判据产出预审报告;扫描件判 UNCERTAIN 转人工。 |
| `stamping-quote-precheck` | 对五金冲压件多供应商报价(文本PDF/XLSX/CSV)做清洗归一与横向拉平,按 E 族判据产出预审报告;料号/起订量方言优先识别;扫描件判 UNCERTAIN 转人工。 |
| `steel-metal-quote-precheck` | 对钢材金属材料多供应商报价(文本PDF/XLSX/CSV)做清洗归一与横向拉平,按 E 族判据产出预审报告;扫描件判 UNCERTAIN 转人工。 |
| `textile-apparel-quote-precheck` | 对纺织服装多供应商报价(文本PDF/XLSX/CSV)做清洗归一与横向拉平,按 E 族判据产出预审报告;扫描件判 UNCERTAIN 转人工。 |
| `wire-cable-quote-precheck` | 对电线电缆多供应商报价(文本PDF/XLSX/CSV)做清洗归一与横向拉平,按 E 族判据产出预审报告;规格型号方言优先识别;扫描件判 UNCERTAIN 转人工。 |

## 安装

```bash
# npm 预构建包（推荐，免构建授权）
dsh plugin --profile web add dsh-quote-precheck-suite

# 或 GitHub 源码仓（纯 JS、无 prepare 脚本，同样免构建授权）
`dsh plugin --profile web add github:toss6969/dsh-quote-precheck-suite`
```

安装后重启 `pnpm dsh web` 生效；技能出现在会话的 `<available_skills>` 目录中，
也可在 `~/.dsh/skills` 视图侧查看。

## 引擎运行前提

- 本机 Python 3（引擎为本地确定性脚本，不经网络）
- 按需 `pip install pyyaml openpyxl pdfplumber`；每个技能自带环境自检：
  `python ./scripts/engine/main.py selfcheck`（退出码 0=可用）

## 使用示例

> 「这三家五金冲压供应商的报价表拉平一下，标出价差和 MOQ 异常」

Agent 会调用对应行业技能，运行随包引擎并产出：横向对照表 / 清洗台账 / 异常清单 /
转人工清单（Markdown 报告 + 机器可读 findings JSON）。

## License

MIT
