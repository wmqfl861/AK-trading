# 2026-10-30 Root 调用错误与恢复经验

本记录区分调用错误、资源拒绝和未完成审核，不能作为产品 PASS。未修改冻结 helper、模型配置或项目规则。

| 情况 | 原始结果 | 原因与处置 |
| --- | --- | --- |
| DISPATCH047 首次 Root memory wrapper | raw 1；未创建子进程，047未消费 | 将 pathlib.Path 传给旧 helper 的 cwd，ctypes CreateProcessW 的第8参数要求字符串；清理分支又引用尚未定义的 peak。保留原 MEMORY 目录和错误，独立 MEMORY-002 使用 REQUEST 中原字符串 cwd，确实启动审核。 |
| 只读定位两次错误 | 各 raw 1 | 猜测不存在的 USAGE.md，以及省略 src/r1 的 common058.py 路径。没有改写输入；改从冻结清单取真实路径。缺失文件的读取不能当成功或 NO_MATCH。 |
| 额外512MiB作者槽准入 | raw 1，启动前停止 | 扣除完整预留后 RAM 余量为 -118018048 B。没有发布容量记录或启动作者。改用已空闲的既有槽，重新测量为正后发布CAP303。 |
| 实际 DISPATCH047 审核 | native child130 / outer130 / Job0，570.6秒 | 达到规定的整体工作上限；8个工具记录均完成，只有脚本，没有运行结果和VERDICT。TRACE/RESULT、CLI TERMINAL未生成。原作者051处置未完成部分，不把该次当PASS或自动重放。 |

后续调用的实际约束：文件系统方法入口使用 Path；旧 Win32 helper 的 cwd 必须是原权威记录中的 str，argv每项也须为str。先核对函数真实契约，不能把“路径归一化”为 Path 错套到原生字符串接口。Root 后续三次作者调用已按此签名启动；M5作者047实际 native0、338.7秒、最终清理Job0。既有 helper 的早期异常清理缺陷仍保留，不在未经计划和审核的情况下改其冻结源码。

可选目录先判断是否存在；必需路径由已钉住的输入记录解析，禁止凭名称拼接后用失败Read探路。先完成前置验证、序列化和空间准入，再创建目标。原始错误保留，后继用独立路径，不能覆盖失败文件或用统一成功退出掩盖错误。

有限 M5 比较的真实命令为冻结 r2/observe_postfit046.py --output runtime/m5-postfit-observe046-001，通过已接受 r19 监督器执行，512MiB/900秒上限，实际17.797秒、native0、Job0。3 passed /0 failed，原输入哈希未变；三组规范字节与拒绝相等，枚举26413→2，耗时减少约10–33%。这是 SYNTHETIC/no-bar/NO_CANDIDATE，不是冷拟合或完整100项验收。

本机证据入口：C:/AKWorktrees/aktr-goal-20261026/zcode-dispatch/dispatch044-completion050-newreview-001/ROOT-NO-LAUNCH-001.json、ROOT-CLOSED-047-001.json；C:/AKM5C019/capacity005/runtime-route-031/runtime/capture-postfit-observe046-001/RESULT.json。公开仓库不含原始会话库、凭据、网络暴露细节或大运行流。
