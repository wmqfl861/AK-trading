# C05 计划意见作者处置

日期：2026-10-25。作者：Astra/max，会话 `01a171b2-3f04-7413-85f6-fb5423ee127d`。有效版本为 [C05-PLAN.md](C05-PLAN.md) 的“2026-10-25 作者处置版（EFFECTIVE）”，立即供 ZCode 开发使用；不设计划 PASS 门槛，不再开计划审核轮次。

仅处置 F1–F4。意见输入为 `D:/Maintenance/aktr-development-20261025/c05-plan-advice-001/OWNER-REPORT.md`（LOCAL_ONLY，3204 字节，SHA-256 `d0b2beffca3e4998a2d9cb425d623ef1bad7c6cc79b97c48777e8272b87e113e`）。据 Root 本次交接，该咨询调用已完成、原生退出 0，审核期间计划输入未变化。本次处置前实读计划为 13635 字节，SHA-256 `35f7c90bd8cecf54f9195f8b78c0d407b6aff07e9e59e1c3e97ce49cab32628b`。咨询意见及其“无阻塞缺陷”结论不构成产品硬审或产品信用；本作者独立决定如下。

| 发现 | 作者决定 | 理由与落实 |
| --- | --- | --- |
| F1：静态枚举与参数解析 | ADOPTED | 处置前计划已要求收集导入前路由，但默认总体/目录如何展开仍可误解。§4.2 明确按固定 pyproject 配置静态枚举、复用 pytest 参数解析并保留显式 node 语义；分歧保留真实 raw5 或未解析记录，不能静默漏项。既有选择回归补充带值选项场景，不引入新测试平台。 |
| F2：父进程中断码 | ADOPTED | §4.4–4.5 锁定既有 KeyboardInterrupt=130，停止后续派发并报告未运行域；子 pytest 自身 raw2 保留原义。启动失败保留诊断和非零启动器结果，不虚构子进程退出码；在现有失败传播回归中断言 130。 |
| F3：basetemp 派生 | ADOPTED | §4.3 固定请求路径下按确定域名派生；每域目录启动前不存在，父目录保留，不交给 pytest 删除。保留已有请求路径拒绝及参数只许一次的规则，既有 basetemp 回归覆盖这些语义。 |
| F4：复制目标已存在 | ADOPTED | §3 明确已存在目标仅可在字节一致后复用，否则拒绝覆盖并报冲突。Root 已有复制实现符合此要求，采纳只澄清既有保护，不改材料、不刷 pin、不要求重做复制。 |

据 Root 本次交接，C05 worktree 与 A∪B 的 87 项本地输入已准备，`C05-LOCAL-INPUTS.json` 记录哈希、零历史材料不匹配及目标存在保护。复制材料一致不等于 DP08 旧 pin 与 tracked collector 一致；原计划中的真实消费差异和其他失败继续保留。Root 将现有 `C05-HANDOFF.json` 绑定本有效计划及实际 ZCode session/store/cwd 后立即激活开发，无需额外审批。本文不虚构尚未绑定的会话，也不要求修改被其他调用固定的派工记录。

产品写集仍仅 `scripts/test_offline.py` 和 `tests/test_offline_launcher.py`；canonical 377 冻结、既有 guard、九项危险节点排除、原完整 `python scripts/test_offline.py -q` 及其真实全量结果报告要求均继续有效。导入隔离完成与完整 C05 基线关闭分开判断；不刷新历史 pin，不修改 M4 活跃输入。本轮仅发布这两份 C05 规划文档，未实施产品、未运行产品测试，未签发成果 PASS。原计划中的事故引用、原生退出码纪律及交易/凭据/配置边界全部保留。
