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

## 09:15 UTC 后继检查与额度限制

- N8 principal060 的CLI/监督器均raw0、Job0，独立结论却为FAIL。必需命令 `run058.py --out-root .../checks060/review-001/focus-001` 与冻结runner第10行只接受`checks058`根的契约冲突，命令raw1，检查子进程未启动、focus目录未创建。成功的记账检查及反例保留，原作者062只处理输出路径与续接契约。以后生成审核命令时必须逐项核对参数接受范围和输出根，不能把“路径是新的”当成“路径被程序允许”，也不能为探路消耗正式入口。
- T7原Sol033已修复两处Popen类契约，原B完成新候选两绑定节点。其检查脚本曾错误假定24个节点等于24个顶层函数，以及读取尚未由M1生成的L0，原失败保留。后继按实际参数化展开核对24节点和64断言；L0始终按未来输出处理，未复制旧锁。两个注入管道失败没有JSON收据的限制未被改写为已验证。
- DISPATCH作者053实际CLI0、输入未变、有效处置已完整发布；receiver/outer raw1来自一次已恢复的SSE idle-timeout事件。保留分层结果，不重复已完成作者调用。现有接收器不会因本条文档变成不同实现。
- T7 R0-016实际CLI/outer raw1、168.47秒、Job0、输入不变，provider明确返回1308/HTTP429，stdout为空。HTTP日期09:15:16 UTC与retry-after2995给出约10:05:11 UTC恢复时点；不是源码或PowerShell退出码转换错误。暂停新的ZCode派单到该时点后，沿原SID/cwd/store续接，不换模型或配置、不盲重试。会话中已有4个工具raw0与1个raw1，共16.177工具秒，须接续真实未完成项；其中审核脚本仍含旧r16候选字面，原审核者应处理，Root不改审核包。
- 错误流可能含HTTP响应头和临时cookie。后续诊断只提取providerCode、HTTP状态、retry-after及安全摘要，原始流保留本地，不把完整响应头打印或提交到公开仓库。

以上均为真实状态和防复发操作约束，不是独立产品PASS或“以后绝不会失败”的保证。

## 10:51 UTC 调用与完成边界补充

- Root 两次只读路径定位错误保留为raw1：工具b2b7e7误拼DISPATCH作者目录；0cb62e假定原SPEC的TRACE/RESULT已生成，实际成功调用使用SPEC-002/TRACE-002。分别改从MAPPING054.authority和实际SPEC-002.output解析并验hash，后继读取raw0。应从有效绑定取得路径，不按目录编号猜测；失败尝试不改记成功。
- Root补正前置检查a6a4bc raw1来自把PID存在当作旧进程仍存活。实际旧CLI及trace进程OpenProcess均返回87；旧外层PID已被系统复用，新创建时间晚于原Job关闭。原Job0仍成立，新进程未受干扰。后继以原创建身份、当前原生创建时间/退出状态和既有Job关闭证据判定；不得据PID数字直接结束进程，也不得凭PID不存在替代完整关闭证据。
- R0-017恢复完成CLI0/Job0、两项节点通过。审核脚本用SystemExit(dict)导致包装层raw1，真实child0和2P分别保留；审核者已明确处置，未重跑成功节点。以后退出值须为整数，结构化结果先序列化输出。
- T7 adapter033审核写出CHECKS/REPORT/VERDICT后继续执行Bash/TodoWrite，达到90.139工具秒后trace/outer130、Job0，1个工具区间未闭、CLI原生终态未知。不能因VERDICT存在就声称整次接受完成。原作者处理有限补正；新审核结论应以实际FileRef生成，发布后直接返回，不再发工具。
- N8 owner003测试用deepcopy复制整个元组时保留了request与模拟native identity的别名，反例实际同时改了两处，产生正确的更早拒绝但检查期望错误。分别复制对象后由独立062验证，32项全部通过；旧31/32/raw1保留。Owner又在剩余工作时间短于批次最大耗时时启动检查，工作超额保留且总120秒全额扣除，关闭额度不得挪作测试。
- DISPATCH已实证旧Proxy成功发布后错误退回125146B临时费用，修正保留gross并追加精确补账。65536B审核hold随后发现没有可用的gross保留写入通道，新055接口实际验证成功；这仍需独立审核。异步命令应在工作预算能覆盖实际执行和结果回收时启动，并及时回收原句柄；owner本次054总194.376/180秒的超额完整保留，不能用后续补充额度回溯消除。

上述是状态与防复发经验；未改规则、已冻结审计包或任何成功的一次性运行记录。

## 11:30 UTC 启动前尺寸与结果字段补充

- DISPATCH055准备检查35019b和d1d9cf分别raw1：完整请求序列化36163B超过暂定32768B；补入必需输入闭包后41838B又超过暂定40000B。两次均在最终REQUEST/SPEC发布及CLI启动前停止，没有消费审核。后继根据实际152项输入完整序列化41804B，配置45056B请求上限，并在原作者81920B外部非时钟上限内作一次明确追加分配；原失败、旧763B超额和已有费用均保留。随后请求发布raw0、实际独立审核已启动。后续先完成最终字段和输入闭包、测量整个序列化结果，再确定具体分配；不要反复猜测请求大小，也不要为塞入旧上限删掉必需pins。
- N8正式068本身已成功，但Root接收准备84b532 raw1：把外层RESULT假定成扁平child_exit字段；实际已接受capture把Job结果放在outcome对象。错误发生在任何写入或新launch之前。读取实际结构后使用outcome.child_exit，后继接收raw0，正式producer没有重跑。接收不同wrapper时须先核对真实结构和字段类型，按该版本的明确路径取值，不能用缺失字段的默认0或通用成功值掩盖差异。
- N8正式068的生成VERDICT表示限定工具范围，且product_review_credit=false；其两个流程107项通过不能自动变成原ZCode最终CLI终态或F02整体PASS。保留真实自动运行与独立审核的区别，按现有严格引用契约接续，不伪造CLI记录。
