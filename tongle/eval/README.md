# eval — 知识质量评测体系

> 给 wiki 注入管道装仪表盘+反馈环：把"注入哪些质量最高"从摸索变成可测可对比可收敛的数字。
> 设计稿见 `docs/eval-design.md`。本目录是实施。

## 北极星
知识/机制有没有让 AI 更懂（对治外部痛点"AI不够理解"）。测知识有效性，非技术对错（机制正确性归 ke `test/`）。

## 三层指标（MVP 先做①）

| # | 指标 | 回答 | 状态 | runner |
|---|------|------|------|--------|
| ① | 注入增益 | 注入哪些质量最高 | ✅ 全量跑通(18痛点) | `runners/inject-gain.py` |
| ② | 采纳率 | 是知识不是信息容器 | ✅ 阶段2a离线MVP(全量跑通) | `runners/adoption-rate.py` |
| ③ | 增益曲线 | 越用越聪明 | ⏳ 未做 | `runners/gain-trend.py` |

> ⚠️ **①注入增益 ≠ 增智评分**（架构范式§九红线⑥防线）：①测消费环 L0/L1「注入有效性」（注入了 AI 更懂吗），
> 不是加工环跨域沉淀「被复用过才算真洞察」的增智判据。gain 数字用于判「注入有效与否」+「策略间对比」，
> **不用于优化排名/刷分**——防退化成「打分漂亮」的新花架子（呼应 6/17 否决 8维 rubric）。

| ④ | 机制组装增益（V2） | 装 skill 增益差 | 🔒 预留 | — |

## ①注入增益 被测口径（指挥官 2026-06-26 拍板）
- **A组**=裸跑（无注入）  **B组**=注入（资产路由清单 + 按痛点匹配的 synthesis.md 内容）
- **judge**：LLM-as-judge 盲评，给 A/B 各打 0-10 质量分，gain=score_b-score_a（正=注入有效）
- 被测模型 `glm-5.2[1m]`（复现 CC 真实配置），judge 用 `doubao-seed-2.0-pro`（异模型降同源偏差）
- 对比 `--strategy all` vs `access` → 增益最高策略 = 质量最高注入
- ✅ 全量首跑（2026-06-30，18痛点）：all +0.83(12/18胜) > access +0.28(7/18胜)。详见 `docs/eval-design.md §全量首跑诊断`（gain 绝对值受 judge 参照系效应影响不可信，方向性信号可信）

## ②采纳率（Level 4 阶段2a 离线 MVP，2026-06-30）
- **复用 eval① results**（已有 B 回答 + matched project），不新跑被测模型 → 比 eval① 快
- **judge 一步做**：从注入 synthesis 提取关键概念(3-6个) + 判 B 回答采纳几个 → 采纳率=M/N
- **判据**：采纳=用概念内容做分析（非仅复述名字）；低采纳率=信息容器（注入了没用）
- judge 同 ① 必须 `doubao-seed-2.0-pro`
- ✅ limit 2 验证：pp-001 0.8 / pp-002 0.6（与 eval① gain 方向一致，判据有效）
- ⚠️ **阶段2a 边界**：本 runner 复用 eval① 离线数据判采纳率，**不读 reuse-log.jsonl**。连接点③真消费方（读 reuse-log 配对真实对话 CC 输出）留**阶段2b**——需扩 Stop hook 捕获 CC 输出摘要（侵入真实对话，待指挥官定）。阶段2a 是判据验证，非连接点③真通

## 结构
```
eval/
  datasets/painpoints.jsonl   # 痛点集(历史对话挖真实痛点,朋友卡点待增量)
  runners/inject-gain.py     # ①注入增益(A/B+匹配+judge+聚合)
  runners/adoption-rate.py   # ②采纳率(阶段2a离线MVP,复用①results判采纳)
  judges/llm_judge.py        # 盲评judge(被import,文件名须下划线非连字符)
  llm_client.py              # 火山方舟anthropic兼容封装(被测/judge双模型)
  results/                   # 历次跑分JSON(可对比)
```

## 跑法
```bash
# KE_DIR = 你的 tongle 安装目录（朋友装后是 plugin 所在路径，如 ~/.claude/skills/tongle）
cd "$KE_DIR"
# judge 必须 doubao（kimi-k2.7-code 等 code 模型盲评空输出，见下"已知局限 6"）
ANTHROPIC_DEFAULT_OPUS_MODEL=doubao-seed-2.0-pro python3 -u eval/runners/inject-gain.py   # 全量痛点
python3 eval/runners/inject-gain.py --limit 2    # 快速验证(前N个)
python3 eval/llm_client.py                        # 自检两模型连通
python3 eval/judges/llm_judge.py                  # 自检judge语义(必须先验judge模型不空输出)
```
离线跑、脚本触发、不干扰日常使用。结果落 `eval/results/gain-<时间戳>.json`（results/ 不进 release，朋友自跑自生成）。
全量跑约 1 小时（18痛点×5次LLM调用），用 `-u` 无缓冲 + 后台跑 + 日志文件追踪进度（上次全缓冲观测失误被杀的教训）。

### 朋友跑 eval 前的准备
eval 调 LLM 走 **anthropic SDK + anthropic 兼容 endpoint**（复用你 CC 的后端配置），需 export 三个 env：
```bash
export ANTHROPIC_BASE_URL=<你的 CC 后端地址>      # 如火山方舟 anthropic 兼容入口
export ANTHROPIC_AUTH_TOKEN=<你的 token>
export ANTHROPIC_MODEL=<被测模型>                  # 默认 glm-5.2[1m]，改成你 CC 实际用的模型
export ANTHROPIC_DEFAULT_OPUS_MODEL=doubao-seed-2.0-pro   # judge 必须 doubao（异模型降同源偏差）
pip install anthropic                              # 唯一外部依赖
```
- **痛点集自带**：`datasets/painpoints.jsonl`（18 条历史对话挖的真实痛点，可增量补你自己的卡点）
- **judge 模型必须 doubao-seed-2.0-pro**：code 模型盲评会空输出（已知局限 6），跑前务必 `python3 eval/judges/llm_judge.py` 自检
- **没配 doubao 怎么办**：judge 换你有的 opus 档异模型也行，但需自验不空输出；同模型自评有同源偏差，人工抽检兜底

## 已知局限（MVP）
1. **痛点匹配**：用 `painpoints.jsonl` 的 `project` 字段 + manifest 关键词匹配，非"CC见清单→LLM判断读哪个"的完整模拟。MVP 够用，后续可上路由LLM。
2. **access 策略对非热门项目**：只注入热度前3，非热门项目痛点匹配不到 synthesis（只注入清单）——这是 access 真实劣势，设计上要测的。
3. **同源偏差**：judge 与被测同走火山 endpoint（不同模型已缓解，人工抽检兜底）。
4. **痛点集规模**：18 条历史挖种子（commit 55afbc0 扩容），朋友卡点增量待指挥官提供。
5. **被 import 的模块文件名须下划线**（`llm_judge.py` 非 `llm-judge.py`，Python 连字符 import 坑）。
6. **judge 模型必须 doubao-seed-2.0-pro**（2026-06-30 实证）：env `ANTHROPIC_DEFAULT_OPUS_MODEL` 被 CC Switch 漂移到 `kimi-k2.7-code` 时 judge 返回空输出（code 模型盲评 thinking 吃光 token，text block 空）→ gain 全 -99。全量跑前必须 `ANTHROPIC_DEFAULT_OPUS_MODEL=doubao-seed-2.0-pro` 覆盖 + `python3 dev/eval/judges/llm_judge.py` 自检验证 judge 真返回有效 JSON（反模式②防线：验消费方证据非"env 已设"）。

## 关联
- 设计稿：`docs/eval-design.md`
- 前置：`hooks/build-asset-manifest.py`（`--strategy` 参数化，被评测管道）
- 被评测对象：wiki 注入 v2.0（`wiki-injection-v2-2026-06-22`）
