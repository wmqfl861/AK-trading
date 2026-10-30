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

## 后续真实结果及防复发补充

- DISPATCH052 的首次 Root 启动 raw1、0.65秒、Job0。TRACE 在已配置的 `clock/LIVE.tmp` 写入时发现父目录不存在；异常收尾又调用同一发布器，因而没有TRACE终态。原 watchdog START-INTENT/START/TERMINAL/RESULT 均未生成，对应时间后的模型调用记录为0，产品命令也为0。错误来自Root漏建必需父目录，不是产品失败。保留失败TRACE/MEMORY；补齐父目录后用独立TRACE-002/MEMORY-002/SPEC-002接同一尚未使用的CLI授权。后继实际native0/Job0/19.8工具秒、独立联合PASS。今后从SPEC中的live_budget_path导出并检查父目录，再启动，不能只创建顶层工作目录。
- 上述恢复的第一次只读诊断自身raw1：对Python traceback中repr转义的路径进行单反斜杠字面匹配，断言失败，发生在任何文件写入前。改为匹配错误类型和文件basename，并以真实文件/模型记录核对，不用traceback的显示转义作路径身份。
- Root曾将JSON模板的ROOT_ACTUAL_BYTES/SHA256占位符当成真实FileRef，导致启动前raw1。模板与真实引用按字段类型区分；实际060对象生成后逐项验证。r4不存在的REVIEW086-MAP猜测读取、未先判存在的N8生成目录枚举也保留为读取错误。必需路径从冻结记录取得；尚未生成的输出先判断存在，后继成功不能抹去前一错误。
- T7工具审核第一调用被Root套了600秒整体上限，实际工具额度并未耗尽；后续在原剩余额度内完成独立审核。工具执行额度与包含模型等待的整体上限分别记录，不能擅自把前者用作后者。
- T7正式full025真实raw1，24项为16P/3F/4E/1S。原Sol在实际栈中定位entry035将subprocess.Popen替换为普通函数，导致Windows asyncio的Popen子类定义TypeError，随后出现base_events导入连带错误。另有negative fixture旧锁路径不存在、v2.1.6与实际v2.1.7绑定不一致。根因已定位，修复及复审尚未完成。新的adapter应保留类/继承契约，并在实际accounting安装后用新进程覆盖子类定义和asyncio/SQLAlchemy导入；不能只验证简单子进程成功。不得改stdlib、放宽M1门或把异常字符串改成当前错误以凑通过。
- N8作者061实际CLI native0、输入未变，接收器因流中一次已恢复的SSE Reconnecting事件返回1；作者有效处置文件已完整发布。分别保留原生退出、接收器退出和产物事实，未把接收器1改写为0，也未重复模型调用。M5原owner最终交接曾因窗口已关闭在I/O前停止，原PowerShell raw1和未生成文件事实同样保留。
- T7独立工具报告的一个辅助wiring031哈希字面有录入错误；主配置、请求、冻结清单的绑定正确，真实依赖与这些已审核输入逐项一致。Root在单独启动记录披露错字及正确来源，未修改审核包或伪称错误字面正确；这不是重新签发独立PASS。

这些记录不承诺以后绝无错误。已发生的每次失败保留原始结果，后继依据实际执行与明确范围判断；最终产品接受仍由独立审核给出。
