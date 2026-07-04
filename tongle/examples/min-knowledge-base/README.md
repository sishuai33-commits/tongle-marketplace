# 最小知识库示例

> 演示 ke 注入链怎么工作。复制到你的环境后，注入链就有东西可读。
> 真实用时把这些示例内容换成你自己的。

## 这里面有什么

```
min-knowledge-base/
├── vault-wiki/                          → 复制到 $WIKI_VAULT_PATH/wiki/
│   ├── .ai-vocab.md                    AI 知识地图（实体/概念/项目表）
│   └── projects/示例项目/synthesis.md   项目认知总览
└── memory/                              → 复制到 $CC_MEMORY_DIR/
    └── working-memory.md                信号汇聚层
```

## 复制命令

```bash
# 假设你在 release 目录下，先 source install 生成的 .ke-env（拿到路径变量）
source .ke-env
mkdir -p "$WIKI_VAULT_PATH/wiki" "$CC_MEMORY_DIR"
cp -R examples/min-knowledge-base/vault-wiki/. "$WIKI_VAULT_PATH/wiki/"
cp examples/min-knowledge-base/memory/working-memory.md "$CC_MEMORY_DIR/"
```

## 复制后会发生什么

新开 claude 会话，SessionStart hook 自动：
1. 读 `.ai-vocab.md` → 注入 wiki 资产路由（CC 知道有"示例项目"等知识域）
2. 读 `working-memory.md` → 检测活跃 topic → 注入"上次会话焦点: 示例项目"
3. 你提到"示例项目"时，CC 会主动读 `projects/示例项目/synthesis.md`

把示例内容换成你自己的项目/概念/笔记即可。
