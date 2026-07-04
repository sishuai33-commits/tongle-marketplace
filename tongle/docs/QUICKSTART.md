# 5 分钟上手

## 1. 装

把 `tongle-dist/` 目录拿到本地（解压 `tongle-dist.zip` 或直接拷贝目录），在任意目录跑：

```bash
claude plugin marketplace add ./tongle-dist
claude plugin install tongle@tongle-marketplace
```

CC 自动处理安装，**不需要手动建 symlink**，跨平台兼容（Windows 不用纠结 `ln -s` 权限）。装完新开 claude 会话即生效。确认装上：`claude plugin list`（应见 tongle）。

环境检测：`bash tongle-dist/tongle/scripts/env-check.sh`，缺啥会提示。

## 2. 初始化最小知识库（让注入链有东西可读）

ke 的注入链读你的 memory（`working-memory.md`）+ wiki（`.ai-vocab.md`）。首次用没有这些，最简单的方式：

**新开 claude 会话，跟 CC 说"帮我初始化最小知识库"** —— CC 会从 plugin 自带的示例复制到你的 memory 和 Vault。

或手动初始化（plugin 装在 `~/.claude/plugins/cache/tongle-marketplace/tongle/<version>/`）：

```bash
# 默认位置可跳过 env；非默认位置在 ~/.zshrc 加 export
export CC_MEMORY_DIR=~/.claude/projects/你的编码/memory
export WIKI_VAULT_PATH=你的Vault路径

PLUGIN_DIR=$(ls -d ~/.claude/plugins/cache/tongle-marketplace/tongle/*/)
mkdir -p "$WIKI_VAULT_PATH/wiki" "$CC_MEMORY_DIR"
cp -R "$PLUGIN_DIR/examples/min-knowledge-base/vault-wiki/." "$WIKI_VAULT_PATH/wiki/"
cp "$PLUGIN_DIR/examples/min-knowledge-base/memory/working-memory.md" "$CC_MEMORY_DIR/"
```

示例库含一个示范项目，让你看到注入链怎么工作。填你自己的内容时照着改即可（详见 `examples/min-knowledge-base/README.md`）。

## 3. 新开 claude 会话验证

```bash
claude
```

新会话启动时 SessionStart hook 自动加载。你应该能看到（CC 上下文里）：
- wiki 资产路由注入（CC 知道有哪些知识域）
- 记忆健康检查（无异常则静默）

跟 CC 说"看全局"或问它"你知道哪些知识域"，能验证注入是否生效。

## 4. 填你自己的东西

- 持仓 / 投资阈值 → `config/invest/thresholds.yaml`（已含示例注释，直接改成你的）
- 你的方法论 → `assets/perspectives/commander.yaml`（已含示例，蒸馏填你的）
- 你的 wiki 笔记 → Vault `wiki/` 下

不用 invest / commander-perspective 的话，对应文件可忽略或删除（见 `assets/manifest.yaml` 说明）。

## 5. 日常用

正常和 claude 对话即可。tongle 不干扰你——注入链自动把你的知识喂给 CC，让它越用越懂你。

- 卡了 → [反馈模板.md](反馈模板.md)
- 详解 → [INSTALL.md](INSTALL.md)
- 是什么 → [给朋友的知识工程介绍.md](给朋友的知识工程介绍.md)

## 6. 判别环裁决：两种模式（按需选）

tongle 会自动采集"你可能想沉淀的认知动作"（读了某个 wiki 页后改代码 / 搜了某个问题 / 对话里的决策），

堆成待裁决候选。候选攒到 ≥3 条，下次开会话 CC 会提醒你裁决。**裁决有两种模式，都合规：**

### 模式 ① 逐条人裁（正道，推荐候选少时用）

在 claude 会话里跟 CC 说"裁决待处理候选"——CC 会从 plugin 目录跑 discriminate-resolve.py，逐条展示候选 evidence 让你填 disposition。

或手动跑（marketplace 装法下 plugin 在 cache 目录）：

```bash
PLUGIN_DIR=$(ls -d ~/.claude/plugins/cache/tongle-marketplace/tongle/*/)
python3 "$PLUGIN_DIR/hooks/discriminate-resolve.py"
```

逐条看候选的 evidence（路径/搜索词/决策片段），你填 disposition（adopt/isolate/discard）+ 关系类型。
守红线⑦原旨：关系/处置**人填非脚本判**。

### 模式 ② 脚本批量 + 标注（图快时用，候选多时）
写个 python 脚本按你的规则批量判 disposition，但**必须**：
- 每条落 `experience` 时带 `source: "batch-<你的标注>-<日期>"` 字段（可审计谁裁的）
- 脚本规则是你定的（人间接判），不是 LLM 自动判
- 跑完抽检几条，发现误判就纠偏改 disposition（人在回路）

> **为什么支持两种**：候选一多逐条裁太累，脚本批量图快是人之常情。但 disposition 是"这条算不算知识"的判断，
> 本质该人定。脚本批量=你把规则写死让脚本代手，规则还是你的。只要带标注可审计、人能纠偏，就合规。
> 别让 LLM 自动判 disposition——那才是越界（红线⑦）。

两种模式产出的经验都进 `discriminate-experience.jsonl`（棘轮只升），下次采集会避开你已 discard 的（⑤回流）。

