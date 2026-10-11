# MathLab Contract-first Planner

Planner 的正式交付物是一个完整 Research Contract，而不是静态 Proof DAG。

Contract 同时包含：

```text
Mathematical Contract
Research Policy
Structural Diagnosis
Detailed Horizon
Structured Horizon
Directional Horizon
Execution Policy and Budget
```

## 三层规划

```text
Detailed → Structured → Directional
```

- Detailed：handoff-ready Node Cards，批准时物化 Research Nodes；
- Structured：方向 + Activation Condition；
- Directional：远期终局/分类/备用路线。

Structured/Directional 不预先获得 Node ID；条件成熟后由 Architecture Reviewer 写完整 Node Card 并 SPAWN。

## Pipeline

```text
persistent Contractor / Reviser
        ↓
Candidate Contract
        ↓
fresh Fidelity Auditor
fresh Completeness Auditor
fresh Rolling-map Auditor
        ↓
fresh Integrator
        ↓
immutable Contract Version
```

所有角色输出可读 Markdown。格式错误一次性聚合，同 persistent thread 最多五轮 repair；数学偷换、越权和伪造证据 hard reject。

正式 Prompt：`prompts/contract/`。
