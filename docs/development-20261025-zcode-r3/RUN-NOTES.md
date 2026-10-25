# 本批运行问题与避免方式

- P1-6首次调用及原会话一次续接均因模型服务HTTP502失败，raw native1、stdout为空，trace分别为`1a0523da-81b8-4132-90ee-30146fc752aa`和`12e34cda-174d-49ed-893e-4260c66d0919`。两次输入均未漂移、候选无代码改动。此事是外部模型请求失败，不是产品测试失败或rg无匹配。原字节日志留在本地，发布摘要不带headers、cookie、请求体或凭据。保留同一SID/store/cwd，先确认前次终止，再有限恢复；重复服务错误应暂列阻塞，不能无限重试、换模型或改配置掩盖。
- replay计划意见调用原生0并产生建议，但作者在调用期间将计划从9273改为9311字节，监督器正确返回98。原失败保留；原Astra作者已对最终文档逐项确认建议适用性并发布处置，没有假报输入哈希一致，也没有把咨询意见升级成产品信用。以后调用前使用作者已停止写入的版本或独立固定快照；不要以“文件已出现”代替“此版本已固定”。这不增加计划PASS门槛。
- 外部Astra workspace-write运行曾被操作系统拒绝读取用户目录中的skill。保留原生1；按已提供的图谱覆盖事实和当前源码作限定后备读取，未改权限/模型配置。以后把任务必要的非敏感skill指引随工作单提供到可读工作区，避免反复探测同一拒绝路径。

后续恢复依据：P2-7硬审与C05计划意见分别于09:24和09:25 UTC真实native0完成；SQL据此启动一次原会话、源码优先的小步骤恢复（p16-owner-003），并非在无新证据时无限重试。其结果仍须单独验证，不能由其他任务成功推定。

- 后续真实进展：`p16-owner-003`源码阶段native0，12项离线通过/3项隔离测试待执行。`p16-owner-004`完成新建独立PostgreSQL集群、直接pytest的15项全通过、角色新建/重复执行和自身集群关闭后，CLI再次因HTTP502/native1终止。原guard入口为12 passed/3 failed，直接隔离入口为15 passed/raw0；保留两者，不削弱guard。Root核对终止、准确四文件写集与实际结果后发布机械冻结收据并立即独立硬审；没有把CLI失败改记成功，也没有重跑测试只为补一份交付文档。
- `c05-owner-001`在HTTP502后native1，trace=`4bc63956-5ca0-4d4e-8da0-413b9827ecbc`，当时尚无产品源码变更。核实前次终止、输入未变、原库及SID后，基于同时间其他调用的实际进展，仅作一次分步恢复`c05-owner-002`，先交真实源码与焦点测试，后续原验收不削减；源码改动已出现。模型/provider配置未改。
- `replay-owner-002`沿原SID恢复后真实native0；完整八文件raw1的14项失败与干净基线相同，均属旧退出绑定hash常量对照。开发者保留失败并冻结两文件提交，独立审核已启动。后续不可通过刷新常量、删除测试或将CLI native0等同产品PASS来掩盖基线问题。

- `replay-hard-review-002`与`p16-hard-review-002`均由原独立审核者续接，真实native0、输入未变，并给出对应窄写集无条件PASS。复用了前次已经完成且输入未变的焦点20项、SQL15项，不因调用恢复重复成功测试；各次HTTP502/native1原件保留。replay独立完整回归仍是191 passed/14旧基线failed，SQL生产采用仍为NOT_RUN。
- replay审核者手写报告时把测试文件SHA-256的一位抄错。Root在提交前逐项比较实际字节、原REQUEST、FREEZE与VERDICT，发现差异；原审核者以`replay-review-pin-correction-003`追加机器生成的更正，native0、输入未变，确认是报告抄写错误，原报告不覆盖、产品不重跑。以后报告中的pins直接由权威记录及实际文件计算生成，禁止手抄长hash。
- Root提交replay前的一次JSON读取（工具记录`d5a18b`）因`Path.read_text()`未指定编码而使用Windows默认GBK，遇UTF-8标点触发`UnicodeDecodeError`，raw1。失败发生在任何git add/commit/push之前，没有部分提交。后继命令显式`encoding="utf-8-sig"`并以`python -B -X utf8 -`运行，完成原冻结字节核对、提交和推送，raw0（`472cc4`，提交`f979e88`）。以后本批所有Python协调命令均启用`-X utf8`，文本读取明确编码；保留原失败，不用忽略解码错误掩盖数据问题。
- 原forward执行作者003明确允许结束旧的未完成审核并重切范围。Root已确认旧watchdog/CLI/helper/worker及已知后代均不存在；最初按原深目录复制快照时，Windows长路径导致`FileNotFoundError`、raw1（`eb8445`），当时没有最终快照清单、closeout或源码写入。保留该部分副本，后继采用`D:/AKSN261025/FWD-R7-002/files/<sha256>.bin`短路径及完整原路径映射，实际核对377源码和458份源/审核成员，raw0（`45a5b1`）。旧规则的三份字节从Git b98ef21精确恢复到快照，现行规则不回滚。以后Windows快照采用短目标或经过验证的长路径API，不机械叠加深前缀；复制失败不宣告冻结解除。
- `forward-recovery-owner-001`于2026-10-25 12:42:57 UTC因服务HTTP502终止，native1、stdout为空、输入未变，新helper尚未产生。实际trace为`31699e39-ce85-40bd-9aed-7388eb80163f`。Root只读本任务独立库的session ID元数据，确认原owner为`sess_43fc3d2f-dc0c-4e1b-858d-28ffe727c1e2`，未查询消息、模型配置或凭据；身份记录在本地ROOT-FAILURE-IDENTITY.json。暂不连续重提相同调用；等待其他真实调用的成功进展后，才有界续接原SID/store/cwd。C05本地完整验证继续，不能把此服务失败当成helper产品FAIL。

- `c05-reviewer-prepare-002`在36.8分钟间隔后的原会话有界续接真实native0，输入未变；这只是源码预检，未运行测试或签发产品verdict。它指出域名扁平化碰撞和无参数规划失败误报空选择两个既有计划缺陷。Root核对具体进程创建身份后受控停止旧验证004的19个进程，监督器保留真实raw15；该完整命令只记STARTED_INCOMPLETE，不记通过，也不把它解释为产品超时。原40项焦点和6010项收集结果保留；同一ZCode owner005修复原两文件。之后先用原独立审核者的廉价反例，再执行原完整验证，不删验收要求。
- 两路OpenCode只读辅助任务在首次JSON事件前被自动快照的全仓`git ls-files --others`拖住，Root仅停止准确绑定的CLI及其git后代，保留raw15，并让watchdog正常捕获/释放。新attempt改在无Git的小工作目录启动，以绝对路径读项目的有限原计划；已核实项目级OpenCode配置文件不存在，未读改模型、provider或持久配置，未加选择器。以后辅助只读核查不要把巨大历史证据目录作为自动快照工作树。

- M5 C1审核准备001于2026-10-25 14:53:46 UTC实际native1、stdout0，615冻结成员及所有输入未变；尚无review root或产品执行。精确trace581937ee-b40a-40f3-8573-5ae9039dabcf记录上游Bad Gateway/HTTP502；原流保留本地，不输出headers/cookie。此事不是测试失败或NO_MATCH。其他多路同窗出现502，暂停盲目增开；等待真实完成证据后，仅允许有界原SID/store/cwd续接，不换模型或配置。DP08随后真实native0完成，不等于服务已彻底恢复。

- DP08 r3首次Git发布检查raw2，仅因已审冻结文件release-verification-summary.md末尾原有空行。未改已审字节、未提交或推送。后继对其余精确写集保持原严格whitespace检查，仅对此一个冻结文档关闭blank-at-eof检查，并核index blob与接受hash一致；该格式例外不豁免产品测试、漂移或其他格式错误。

- Root接续时误将RUN-NOTES.md定位到Maintenance根，Get-Content原生1；实际文件位于docs/development-20261025-zcode-r3。只读失败没有修改产品。后继先枚举已知小目录确认路径再读取，raw0；今后从已发布路径取得输入，对可选发现先检查存在性，不把推测路径当必需文件批量读取。
