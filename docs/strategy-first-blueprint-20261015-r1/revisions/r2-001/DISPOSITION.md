# 原计划作者审后处置：r2-001

原作者续接，2026-10-15。冻结 r1 三文件及[独立 ZCode 原输出](../../zcode-plan-review-001/stdout.json)只读；原 session `sess_eaf1bd29-d103-49ea-9cde-9dd682ad8317`、native0、无条件 planning PASS 和 reviewer 的 hash NOT_RUN 原样保留。本处置不追改原审查，不增加产品信用。

| 事项 | 处置 | 理由与落点 |
| --- | --- | --- |
| 非阻断 1：P2/full100 预算 | PARTIAL | 采纳 pin 权威记录的建议，纠正“统一采用 300s”的推断。P2 是独立命令 300s deadline；HANDOFF-002 的 full100 为 3600s、2 GiB、1 GiB。二者不能混用或据此判定矛盾；见修复顺序 §3。 |
| 非阻断 2：experiments.py 归属 | ADOPTED | M5 保持唯一 writer，R5 顺序续接；不同 owner 必须先显式 file transfer，活动审阅写冻结不变；见实施计划 §1、§5。 |
| 非阻断 3：exit_bindings 语义 | ADOPTED | 缺必填 exit_registry_sha256 才触发该 TypeError；exit_bindings 默认 ()，缺失属于绑定身份完整性问题，分别验反例；见实施计划 §5、修复顺序 §6。 |
| 实质修正 1：双频与来源排程 | ADOPTED | MINUTE/DAILY 各自 ready 立即运行并交报告，双频总批仍需两份；P2/full100 候选门只绑定实际消费者。按旧蓝图事前覆盖/成熟/可用性规则选源，RB 不足考察其他品种，不按收益选择；见蓝图 §1、§3。 |
| 实质修正 2：模拟与真实执行边界 | ADOPTED | 历史回放/本地虚拟撮合可先做 RESEARCH_EVALUATION；前瞻 paper/shadow 在消费代码、输入和运行权限具备后开始记录。20 日等属于完整 SHADOW_ACCEPTED 门，统计 FAIL/INSUFFICIENT 的 SHADOW_OPERATIONAL 仍 NO_TRADE。AGENTS §4 的 execution hard PASS、部署/模拟验收和用户授权/UI 门用于真实订单/撤单；见蓝图 §4。 |

六品种、七族、八期限、首批三族、四政策、六场景、252/60/60、PIT、purge/embargo、连续账本及永久留出保护沿旧合同保留；首批明确 `walk-forward/final_holdout=false`，最终 holdout 另阶段。公共源按实际消费者及原 M1 集成顺序处理，scoped DAILY 复用已修实现，只闭必要流程项。报告统计状态与产品硬审 PASS 分轴，不新增平台、工单或封存体系。

本次只写本目录四文件。一次缩写路径 Read 不存在、一次 docs 文件定位 30s 超时、一次 PowerShell 预检语法失败已保留在工具原输出；随后以权威路径/Root 确认续接，不计产品测试或新审批门。

用户已授权本次排程修订；原产品开发 pause 继续，恢复另等用户指令。本版实质变化由 Root 交一次必要窄 ZCode 规划复审；没有启动产品实现、测试、fit、模拟、SDK、数据库、网络、客户端、部署或真实交易，也不宣称 r2 已取得 ZCode PASS。
