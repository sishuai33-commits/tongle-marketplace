# wiki-management — LLM Wiki Lifecycle Manager

> Full-lifecycle knowledge base management based on Karpathy's LLM Wiki architecture.

**Ingest → Query → Lint → Dream → Forget → Slow Loop**

Manages a Markdown-based Wiki vault through its entire lifecycle: structured knowledge ingestion, automated health checks, background "dream" maintenance with Ebbinghaus-style decay, intelligent forgetting, and slow-loop deep purification.

## Quick Install

```bash
# Clone/copy to your skills library
cp -r wiki-management ~/Documents/My_Skills_Library/self-built/

# Activate (symlink)
ln -s ~/Documents/My_Skills_Library/self-built/wiki-management ~/.claude/skills/wiki-management
```

## Prerequisites

- A Wiki vault with the standard directory structure (use `wiki-bootstrap` to create one)
- Python 3 with `pyyaml` (`pip install pyyaml`)
- Claude Code with Obsidian MCP server configured

## Configuration

### Wiki vault path

```bash
# Default: ~/Documents/Obsidian Vault/wiki
export WIKI_VAULT_PATH="/path/to/your/wiki/vault"
```

All scripts (`wiki_checks.py`, `wiki-lint.sh`, `session-start.sh`) respect this env var, falling back to the default if unset.

### Decay parameters

All decay parameters (half-life, grace period, type weights, link factor thresholds) live in `wiki/.dream-config.md` frontmatter — the single source of truth. `wiki_checks.py` reads them at startup. No duplication.

## What You Get

| Feature | What It Does |
|---------|-------------|
| **Ingest (2-Stage)** | Stage 1 analyzes structure → Stage 2 generates pages. Prevents "write-then-fix" cycles. |
| **Query** | Semantic + keyword search via Obsidian MCP. Auto-updates access_count (memory reconsolidation). |
| **Lint** | `wiki_checks.py` checks frontmatter completeness, dead links, staleness, relevance. `wiki-lint.sh` wraps it for CI/hooks. |
| **Dream** | Background maintenance with 3-gate trigger (time ≥24h, sessions ≥3, no lock). 4-phase: Orient → Gather → Consolidate → Prune. |
| **Forget** | Ebbinghaus-style decay model. Pinned pages exempt. 7-day buffer before archival. 90-day grace before deletion. |
| **Slow Loop** | Cross-page deep purification: detects overlap, contradiction, staleness. Outputs structured work orders for human approval. |
| **LDR Enhanced** | Semantic search + contradiction detection via Local Deep Research integration. |

## Directory Convention

```
wiki/                           # Your Wiki vault ($WIKI_VAULT_PATH)
├── index.md                    # Human-readable index
├── .ai-vocab.md                # Machine-readable vocabulary
├── .dream-config.md            # Decay params + Dream config (single source of truth)
├── .lint-rules.md              # 5 hard lint rules
├── review-queue.md             # 7-type work order queue
├── entities/                   # Cross-project entities
├── concepts/                   # Cross-project concepts
├── projects/{project}/         # synthesis.md + events/
├── queries/                    # High-value query results
├── procedures/                 # SOPs + checklists
├── sources/                    # Source summaries
└── _archived/                  # Sunset archives
```

## Companion Skills

| Skill | Role |
|-------|------|
| `wiki-bootstrap` | Creates a new Wiki vault with standard structure |
| `wiki-full-compile` | Workflow-based full recompilation (~25-30 parallel agents) |
| `wiki-slow-loop` | Workflow-based deep cross-page purification |
| `skills-management` | Manages skills lifecycle (including this one) |

## Key Design Decisions

- **Scripts for facts, AI for judgment**: `wiki_checks.py` mechanically detects issues; AI decides what to do about them.
- **Config lives in the vault**: `.dream-config.md` and `.lint-rules.md` are inside the Wiki vault (like `.git/config`). Each vault is self-contained.
- **Decay, not delete**: Low-relevance pages sunset to `_archived/`, not deleted. 90-day grace period.
- **Two-stage ingest**: Structure analysis before page generation prevents "write-then-fix" drift.

## License

MIT © 2026
