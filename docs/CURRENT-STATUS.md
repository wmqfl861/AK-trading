本周能看到什么：七年RB策略报告已入库。
卡在哪：换行设置与真实输入区间边界。
需要我做什么：私有仓库、换令牌、收紧入口、开长路径。

在有观测的原始年份中，ALL 正净额年数为唐奇安 1/7、均线 3/7、Z 回归 0/7；PRIMARY 正净额年数为唐奇安 2/7、均线 3/7、Z 回归 0/7。
ALL 的唐奇安最差年 2021 年 -121247.923 元；均线最差年 2021 年 -53740.537 元；Z 回归最差年 2021 年 -47132.908 元（并列时列最早年）。
这些是 SCENARIO 描述结果，不是样本外检验、历史实盘或组合收益，不能由此给出交易建议。

# 当前状态（第五轮第二批，2026-11-13）

本批以 [DECISIONS](repo-audit-20261113-claude/DECISIONS.md) J1–J18 和 [NEXT-BATCH](repo-audit-20261113-claude/NEXT-BATCH.md) 为授权；全部完成或被阻塞后停止。外审分支已仅新增10个指定目录文件并快进合入，HEAD `79d9134cdcdead75d98e83e894080a5567c15130`。外审意见不是产品硬审。现行分工只看 PROJECT-MODEL-ROLES 顶部：计划/开发 Astra/max，独立审核/辅助 Sol6.1/max。

| 本机复现（固定Python 3.13环境） | 原始退出 | 实测结果与外审差异 |
| --- | ---: | --- |
| Z01 跨周期可达性 | 1 | 14P/16F/0E，与外审一致；中性标签1日15.7%、20日1.8%。 |
| Z02 时钟元组身份 | 0 | 5/20/60/250日新建与复用耗时比51/230/836/4641；本机绝对耗时不同，重复构建现象一致。 |
| Z09 标签缓存身份 | 1 | 50次地址复用、50次返回旧分钟格，与外审一致。 |
| Z06 分年工具 | 1 | 36P/63F/7E，106项；外审列63F/7E，当前Windows另外36项通过。 |

四条实际命令（解释器均为 `C:/AKR41110/venv-full/Scripts/python.exe`，cwd为仓库根）：

```text
python scripts/test_offline.py -q -s docs/repo-audit-20261113-claude/probes/test_cross_timeframe_reachability_probe.py
python docs/repo-audit-20261113-claude/probes/clock_tuple_identity_bench.py
python docs/repo-audit-20261113-claude/probes/label_session_cache_identity_probe.py
python scripts/test_offline.py -q tests/experiments/test_historical_daily_chronological_transfer.py
```

原始输出及压缩件：`C:/AKH71113/reproduction01/RESULT.json`及同目录。四作业均自然终态，无超时；预期探针失败按原始码保留，不作产品PASS。

| 任务 | 当前状态 / 下一步 |
| --- | --- |
| S1 | 可做部分已提交并推送`43c44e0`；CI 43353862057成功，1216P/4skip/0F。两个已合入外审分支已删除。`.gitattributes`单项停止，见待决。 |
| K40-A | [七年报告](research/v1-daily-baseline-rb.md)已推送`a80db5c`，独立无条件PASS、24P/raw0、4反例通过；CI 43364742379成功，1303P/4skip。 |
| S2 | `fd8f1b3`已推送；r6原字节入库，定向87P/raw0；CI 43356267610成功，1303P/4skip/0F；r5保留。 |
| K02b | [测量表](research/k02b-cross-timeframe.md)已推送`3ea5b32`、CI 43363683356成功：获准v1样本中性标签1步14.42%、20步2.37%，raw0；其余三项无合法模型/快照，NA而非0。 |
| K42 | [就绪清单](research/k42-real-input-readiness.md)已推送`0e4394b`，CI 43365962196成功（1303P/4skip）；2/2候选缺安全非holdout边界，生产测量BLOCKED；60/60分片manifest一致，价格打开数0，metadata raw0。 |
| K10b / K12 | K10b意见已采纳，三文件正式开发中；r6 BEFORE与独立小回放期望已核验，冻结后一次完整AFTER。K12等其合入。 |
| K25 | [分诊表](research/k25-baseline-triage.md)已推送`7e1d026`，CI 43369079502成功：31文件、213/213异常匹配，145F/60E/8CE；研究路径110F/16E；生成raw0，不改测试。 |
| J7 | [分级变更申请](contract-lock-levels-change-request-20261113.md)随本提交入库：PROPOSED/NOT_IMPLEMENTED；不作当前放行依据，K12仍用现行完整锁。 |
| K40-B | 原作者短计划已写、Sol意见进行中；新入口显式绑定原4份authority和10/10已接受冻结引擎，旧wrapper/pins不改；尚未运行新矩阵。 |

**方法裁定差异**：K02b仅标签占比回退到原已授权v1 real-input-02；外建议的回退原因为quality/time_basis，本次真实原因为r1/r2无法安全证明非holdout边界。作者明确不新增来源、不读未知区间、不解锁其余指标。

**全量基线**：r6 6293 ran / 6055P / 145F / 60E / 33skip / 8CE，原始退出1；上批严格比较新增F/E/CE=0/0/0、独立PASS。K10b候选已在开发，尚无完整AFTER；本批将前移为r6对照，旧失败不隐藏，不新增skip/xfail。

**待决**：r1/r2需明确可验证的非holdout起止/成员范围，当前不加载价格；就绪表保留旧声明与本次测量的区别。另，S1添加`* -text`前后Git状态受stat缓存影响，逐文件原始字节核验发现新合入的10份外审文件工作区CRLF、HEAD为LF，`core.autocrlf=true`；仅撤销本次新增的`.gitattributes`，未修改外审文件/持久配置/执行规范化。按S1停止该单项，后续需决定采用无换行转换的新检出完成此设置；其它任务继续。证据`C:/AKH71113/s1-gitattributes-stop01.json`。

**用户专属未确认事项**：仓库转私有；轮换行情令牌并重启服务；收紧公网入口；Windows长路径；可选分支保护、实际资金与研究100万元假设差异、遗留`chatgpt/rw-probe-b75d2205-a7f9c2`分支由用户处理。没有替用户执行这些操作。

**范围与安全**：H7取消由K40替代；H6并入以后v2日线；H5旧FAIL保留、不新冷链；H8由K12替代，09不恢复/10不启动。K11/K13/K04b/K41、T7重新冻结、锁分级实施、N8/DISPATCH/Forward/paper均不做。公开仓库仅推汇总与结论，输入包/逐笔明细/候选分支/验收清单/锁不上远端。保持enable_trading=False、observer=hold、broker=NOT_RUN、真实send/cancel=0/0；不读取凭据、不消耗最终holdout。
