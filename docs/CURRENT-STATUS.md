本周：六品种报告已出，产品独审通过。
卡点：S4独立环境安装未完成。
请你：仓库转私有、换令牌、收紧入口、开长路径。

五个新增品种都完成了固定条件下的日线模拟，螺纹钢沿用此前接受的结果。
首表使用相同初始资金和基准成本，分别列出主力归集与全部独立合约的净额。
规格采用已核实的静态合约值；前根持仓量的历史可知时点仍未得到验证。
主力列拼接的是各原账户当日盈亏，不是一笔资本连续执行的换月回测。
结果为情景描述，统计资格未评定，也没有据此调参或建议交易。

# 第六轮外审第三批：发布收尾

本批仅S3、K43、K14、K44、K42b、S4、K45，完成或阻塞后停止。外审分支仅新增9文件，fetch/merge均raw0，合入HEAD `af9436fe77b8f75f9043a4c55121bd1a120cb376`。现行分工为Astra/max计划与开发、Sol6.1/max独审及辅助。原始压缩日志、输入及审核包均留仓库外`C:/AKH81113/`，以下为交接摘要。

| 任务 | 状态、提交与验证 |
| --- | --- |
| S3 | 完成：`ab65e45`的`* -text`、`c61f772`的两处规则替换/CI三文件。52个仅换行文件恢复HEAD字节；无renormalize。CI43427609055成功，1381P/4skip/raw0。 |
| K43 | 完成：`9608b5c`，独审无条件PASS；97P/raw0，130318独立比较/0失败，8组反例；CI43436732625成功，1428P/4skip。报告[v1-daily-baseline-rb.md](research/v1-daily-baseline-rb.md)。 |
| K14 | 独立无条件PASS；本提交含两个冻结文件及新增测试CI接线，推送/CI回读进行中。93P、完整M1–M4 1006P均raw0；669差分断言，26回放产物字节不变；最终完整回归零新增。 |
| K44 | 独立无条件PASS，49文件待本批下一项提交。最终r2工具与r1实际生成结果来源分别保留；195P/raw0、78守卫反例；两次28400账户/170470产物/7CHECK字节差异0；253816634项独立Fraction检查/0失败，未用owner数学函数。报告[v1-daily-baseline-universe.md](research/v1-daily-baseline-universe.md)。 |
| K42b | 完成：`50154b5`，CI43442336144成功，1428P/4skip。测量raw0：1783377可用分钟、452日、累计bar×close放行64.431955%，当节cohort98.837094%；594次生产集合相同。表[k42-real-input-readiness.md](research/k42-real-input-readiness.md)。 |
| S4 | 阻塞，记录已推送`b10a4ee`，CI43451659023成功。独立venv创建raw0，安装两次raw1（后次1800.006秒超时）。鉴权启动0次，P/F/E/skip均NA；未改依赖、测试、守卫或基准环境，不继续重试。 |
| K45 | 完成设计：`ba26032`，CI43449548520成功。一次Sol意见5项全采纳；只设计，未真实拟合/评分。[k45-forecast-skill-design.md](research/k45-forecast-skill-design.md)。 |

**开工复现**：cwd仓库根，命令前缀`C:/AKR41110/venv-full/Scripts/python.exe -B -X utf8 docs/repo-audit-20261113-r6-claude/probes/`。四组实际五条，非pytest不虚报通过数。

| 命令尾部 | 原始退出 | 实测 |
| --- | ---: | --- |
| `k40_published_tables_recheck.py` | 0 | 7195检查/0失败，566引用；1.605秒 |
| `pipeline_context_reuse_bench.py` | 0 | 40/120/250日1.2/1.5/1.9倍；热路径新增验证/物化0/0；94.948秒 |
| `empirical_apply_scaling_bench.py` | 1 | Z13复现：100k/H60=6383.350ms，第二hash3174.1ms；修复后原探针raw0 |
| `context_cost_profile.py MINUTE 250 TREND` | 0 | 333.2ms/context；模型/特征/hash份额65.0%/25.9%/0.4% |
| `context_cost_profile.py DAILY 250 TREND` | 0 | 158.4ms/context；模型/特征/hash份额29.2%/65.2%/0.5% |

外审差异：本机Windows耗时高于外审Linux；结论按实测，不视为数值等价失败。K14后测MINUTE127.8ms（份额2.8%/65.5%/8.8%）、DAILY284.4ms（1.5%/86.9%/5.1%），原probe raw0/0；后测有其他并发，不能宣称普遍加速或确定日线变慢原因。份额来自独立profile循环，不能乘plain毫秒冒充绝对组件耗时。

**完整回归**：两侧均用同Python3.13.15、74包版本、8GiB原监督器及离线入口。原226测试文件逐字节保留，新增K14/K43/K44=92/47/98共237节点。命令为`python -B -X utf8 scripts/test_offline.py -q --continue-on-collection-errors --basetemp <独立目录>`，前测src01/tb01，最终确认src03/ta03。

| 完整运行 | P | F | E | skip | 收集错误文件 | selected | 原始退出 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 旧r7（不含K40-B26项） | 6107 | 145 | 60 | 33 | 8 | 6345 | 1 |
| 本批BEFORE（6471.89秒） | 6133 | 145 | 60 | 33 | 8 | 6371 | 1 |
| AFTER02（6311.63秒，失败保留） | 6369 | 146 | 60 | 33 | 8 | 6608 | 1 |
| 最终AFTER03（5459.68秒） | 6370 | 145 | 60 | 33 | 8 | 6608 | 1 |

最终record/compare/assembler均raw0：新增及消失F/E/CE全0，旧nodes/files/domains无丢失，unresolved0；237新节点纳入且不在F/E，完整M1–M4同1006节点/F/E0。四成员另含confirmation9P/raw0、duplicate9P/raw0、range5P6F/raw1；三个同src03探针原样复用并保留实际run ID，四成员总6637selected。新r8基线待随K44入库，旧基线不覆盖。

**保留的失败及作者裁定**：K44-F01在r1确实FAIL（输出允许置于输入包内）；r2仅修目录守卫及29条回归，非守卫53定义/config不变，四小矩阵各2478文件字节一致，独审确认修复。实际两矩阵输出从未重叠，按作者001裁定复用原r1真实结果，CHECK不改签r2。src02过期后测受控停为PARTIAL/raw130，自动assembler2拒绝残缺，未冒充基线。AFTER02唯一新增为`test_internal_decode_defect_still_500_not_widened_400`在收HTTP状态行前超时；D1单例1P、D2整文件141P仅诊断。按原作者限定的一次低并发完整AFTER03确认后零新增；原FAIL永久保留，原因/归因仍NOT_ESTABLISHED，未改原5秒超时、断言或源码。

**其他裁定与外审差异**：K43/K44只用前根已存完整bar持仓量，历史可知时点未证，标SCENARIO，不冒称PIT。K14拟合自产样本走不可变tuple缓存，外来可变图保持安全回退与别名语义，JSON/hash/数值不变。K42b首测raw3因v2.1.2封套被正式v2.1.3 loader拒绝；作者仅批准保原字段/版本的记录测量适配，正式loader仍REJECTED_VERSION，不授权K45拟合。

**K45固定设计**：两模式均r2；TRAIN2023-01-03—2024-02-29，CAL2024-03-01—2024-05-31，TEST2024-06-03—2024-10-18，成熟尾2024-10-21—2024-11-15（不新增决策）。分钟原每节第5格；候选上界27739分钟/431日线锚点、419964请求。Brier八期限家族控制、logloss/校准对照一致、非UNCONDITIONAL胜者和至少60共同评分日；较外部“任一期限95%下限>0”建议更严格。合成context算术参考61.12701分钟，1/3/10倍规划情景1.02/3.06/10.19小时；真实fit/label/IO/bootstrap工时未测，不承诺完整研究约一小时。

**S4命令**：`C:/AKH81113/measurements/s4-r1/venv/Scripts/python.exe -I -B -m pip --isolated install --disable-pip-version-check --index-url https://pypi.org/simple -r D:/Projects/AK-trading/requirements.txt pytest`；第二次child raw1/WALL_CLOCK_TIMEOUT，监督记录raw0。见外置`s4-r2/RESULT.json`，鉴权NOT_RUN不当测试FAIL。

**独立最终结论**：K14 `reviews/k14/hard-review-r1/final-001/VERDICT.json` SHA256 `a957f831d0c24a32264da1147c4527e9251a80afadb078203497b663fccee918`；K44 `reviews/k44/hard-review-final-r1/VERDICT.json` SHA256 `ca2f2aaba95a38be28ac898134134a66faa014cfb77116f9b8af81784f1e78e1`（均相对外置根`C:/AKH81113/`，均无条件PASS）。当前发布仍待main/CI回读，产品审核与整批集成事实分别记录。

**待决**：无DECISIONS未覆盖且需要用户决定的新事项。S4依赖安装阻塞留给后续授权批次，不自动开修复计划。

**用户专属尚未确认完成**：仓库转私有；轮换行情服务令牌并重启；收紧公网入口；Windows长路径；可选分支保护；实际资金是否偏离研究100万元；遗留分支处置。继续由用户本人操作。

**安全及停止边界**：无最终holdout使用；新导出均截止2026-09-04。只授权脚本按env-file路径自行读取连接串，agent不看/打印/修改凭据。公开仓库只推工具/测试/配置/汇总，不推输入、逐笔、候选分支、合同锁、验收包。enable_trading=False、observer=hold、broker=NOT_RUN、真实send/cancel=0/0。H5/H6/H7、N8/DISPATCH、shadow/Forward/paper及暂停工程项均未恢复。
