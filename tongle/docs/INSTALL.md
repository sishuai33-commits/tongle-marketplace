# 安装与生命周期

> tongle 是 Claude Code 插件——把你的理解沉淀成 AI 能读、能记、能用的东西。
> 这是什么 → [给朋友的知识工程介绍.md](给朋友的知识工程介绍.md) ｜ 5 分钟上手 → [QUICKSTART.md](QUICKSTART.md)

## 一句话安装（推荐：CC 官方 plugin marketplace）

把 `tongle-dist/` 目录拿到本地（解压 `tongle-dist.zip` 或直接拷贝目录），在任意目录跑：

```bash
claude plugin marketplace add ./tongle-dist
claude plugin install tongle@tongle-marketplace
```

CC 自动处理安装（plugin 装到 `~/.claude/plugins/cache/`），**不需要手动建 symlink**——这是 CC 官方机制，跨平台兼容（Windows 不用纠结 `ln -s` 权限）。装完新开 claude 会话即生效。

确认装上：`claude plugin list`（应见 tongle）。

## ⚠️ Windows 用户

走 marketplace 装法后，**分发层兼容性 CC 已兜底**（不再需要手动 symlink）。但 hook 脚本仍是 bash/python3，Windows 原生跑需要：

- **推荐：在 WSL 2 里用 claude**（最稳，linux 全兼容）—— WSL 里 `npm install -g @anthropic-ai/claude-code`，再 `marketplace add` + `install`
- **装了 Git for Windows 的原生用户**：CC 触发 hook 走 Git Bash 的 bash；`python3` 命令名可能有坑

macOS / Linux 用户直接装，无此问题。不确定环境跑 `bash scripts/env-check.sh` 看检测。

## 前置条件

- **Claude Code**（`claude` CLI，v2.1+ 支持 `claude plugin` 命令）—— 必须先装
- **python3** —— hook 脚本依赖
- **node** —— pre-security 守卫依赖（装过 CC 即自带）
- （可选）**Obsidian** + Vault —— wiki 知识域，不用也能跑

`bash scripts/env-check.sh` 逐项检测缺啥引导补。

## 环境变量（非默认位置才需设置）

hook 默认按 `$HOME` 推导路径。如果你的 memory / Vault / 项目根不在默认位置，在 `~/.zshrc`（或 Windows 的环境变量）加：

```bash
export CC_MEMORY_DIR=~/.claude/projects/你的编码/memory
export WIKI_VAULT_PATH=你的Vault路径
export SOURCE_ROOT=你的项目根目录   # 采集环源1 扫描根，可选
```

默认位置：
- `CC_MEMORY_DIR` = `~/.claude/projects/<HOME编码>/memory`
- `WIKI_VAULT_PATH` = `~/Documents/Obsidian Vault`
- `SOURCE_ROOT` = `~/Documents/My_Code_Projects`（示例默认值，你的项目根若在别处务必覆盖）

`bash scripts/env-check.sh` 会推导并告诉你设没设对。

## 可选：wiki-management / skills-management skill

tongle 可独立运行（注入链 + 守卫不依赖外部 skill），但装了这两个 skill 功能更全：
- **wiki-management**（Wiki 编译/维护）：tongle 的 SessionStart 会调它的 `wiki_checks.py` 做 wiki 健康检查。没装则跳过。
- **skills-management**（skills 全周期管理）：`config/skills-management/resident-skills.yaml` 是它的配置。没装则忽略。

## 卸载

```bash
claude plugin uninstall tongle@tongle-marketplace
```

或交互式 `/plugin` 界面卸载。tongle 运行时状态在 `~/.claude/instincts/`（observations/probe/cost-state 等），卸载后可手动清：`rm -rf ~/.claude/instincts/{observations.jsonl,.ke-plugin-probe,.cost-state}`（你填的 config/assets 不动）。

## 升级

```bash
claude plugin marketplace update tongle-marketplace   # 拉新版清单
claude plugin install tongle@tongle-marketplace       # 重装（version bump 才会更新）
```

⚠️ 升级前备份你改过的文件（`config/invest/thresholds.yaml`、`assets/perspectives/commander.yaml`）——新版这些是示例注释，你的值需手动迁移（diff 比对）。plugin 装到 cache，你改的 config 不在 plugin 内（在 `~/.claude/plugins/data/` 或你设的 env 路径），升级不会覆盖你的数据。

## 备选：旧自造分发（manage.sh，无 marketplace 时用）

若你的 CC 版本不支持 `claude plugin` 命令，或想本地开发即时生效，可用旧的 symlink 方式：

```bash
bash manage.sh install        # symlink ~/.claude/skills/tongle → 当前目录
bash manage.sh uninstall
bash manage.sh status
```

manage.sh 是 v1.0 时代的自造分发，**主推 marketplace 装法**，manage.sh 留作降级方案。

## 排障

| 问题 | 解法 |
|------|------|
| `claude plugin` 命令不存在 | CC 版本太旧，升级 CC 到 v2.1+，或用 manage.sh 降级装法 |
| hook 没生效 | hook 会话级加载，新开 claude 会话才触发。`claude plugin list` 确认装上 |
| memory/Vault 读不到 | 检查 env 变量，`bash scripts/env-check.sh` 重新评估 |
| 装完报错 | 跑 `bash scripts/env-check.sh` 看失败项，通常缺 python3 或 CC 没装 |

## 反馈

卡了或有问题 → 按结构化模板反馈：[反馈模板.md](反馈模板.md)
