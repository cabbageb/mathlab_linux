# MathLab Dynamic Prover

Dynamic Prover 在不可变 Research Contract 下运行两级异步循环：

```text
Local Research Cell Evolution
+
Global Research Architecture Evolution
```

## Research Node

正式 Node 只有：

```text
Discovery
Claim
```

不设置独立 Closure/Support/Integration Node。

## Node Scheduler

- 所有 Node 先进入 ready queue；
- 全局 active Node 上限默认 10；
- Node slot 释放后 FIFO 补入；
- SPAWN/MERGE 不能绕过 scheduler。

## Research Cell

- 每 Node 最多 7 个稳定 Worker identities；
- Literature / Construction / General Proof / Cross-domain 各至少一名；
- 同 Persona 可多名；
- thread 绑定 worker_id；Assignment 是同一学生的新任务版本；
- 不建立 future-task pending backlog。

## Local Streaming Review

每两份新 Worker report 或重大事件触发。Supervisor 可复用/改派/新增空余 Worker、形成 Finding、提 Promotion、提交完整 candidate，但不能直接 mutate 全局图。

## Architecture Reviewer

正式 action：

```text
CONTINUE
SPAWN_DISCOVERY_NODE
SPAWN_CLAIM_NODE
MERGE_NODES
RETIRE
REQUEST_FRESH_VERIFICATION
COMPLETE_RUN
```

Promotion 是稀缺资源；能留在原 Node 深化时优先不 SPAWN。

## Verification / Writer

Verifier fresh，只核验完整 candidate。所有 Run 终局都经过 English TeX Writer、Controller XeLaTeX 编译、fresh Auditor 和同线程 Writer revision；最终原子发布 PDF 与单文件 TeX。FULLY_RESOLVED 还需 Controller completeness gate。

正式 Prompt：`prompts/vnext/`。
