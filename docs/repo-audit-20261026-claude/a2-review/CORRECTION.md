# CORRECTION.md — A2 判定工件单字段勘误（a2-verdict-correction-002，2026-10-26）

- 席位：同一独立评审席（sess_89678d66）。本勘误**仅更正本席自己的判定工件**中的一个人工转录哈希；产品、测试、评审结论均不变。
- 更正字段：`frozen_write_set.m4_final_sha256`（原 VERDICT.json，`zcode-dispatch/a2-hard-review-001/`，sha256 `f37930ee…39b0`，**原件原样保全**）。
- 错误值（人工转录）：`d62959fbd641593cb4b9c6fe686ba951fcd9e2eed5eb62047823f930c1b8200294`。
- 权威值（机器取自 `a2-owner-eol-004/evidence/SOURCE-HASHES.json` 的 write_set 条目，未手打）：`d62959fbd641593c00e68e4cfb433dd7de26e70d6e99fe8f259d1c3648cbc888`（`tests/test_v2_m4_payoffs.py`，77,874 B，eol-004 字节恢复后的最终冻结）。

**事实不变**：候选 11 文件与本席硬审的 before/after 针、权威冻结在勘误时程序化复核全部一致（`postwrite-checks.json` 的 `all_11_live_files_match_freeze: true`）；原判定的全部测试事实（26/110/9/308、962=948P+14F raw1、反例 17/17 r2、14 项逐节点同因失败）未重跑、未改变。**更正后判定仍为无条件 PASS（限 A2 有效计划范围）**——错误只在判定文本的哈希字段，不在任何被审对象或验证事实。

**成因与防再发**：成因是人工转录哈希（前缀正确的凭记忆补全）。防再发 = 一律程序化复制权威值 + 写后绑定检查（本次新判定即按此构建：完整 JSON 先序列化、临时同胞 + 原子替换、写后重解析并断言与冻结条目、活体候选文件、FACTS 权威值、封存 before/after 针逐一相等，`build_corrected_verdict.py` 全绿）。勘误过程中一次调试单行命令里本席再次手打错值（两哈希嵌合体），被程序化取值当场揭穿——恰好实证该防再发规则的必要性；该调试行未进入任何工件。

**交付物（仅本 attempt 目录）**：
- `VERDICT.json` — 更正后判定（sha256 `02341079a5e3b76850a42296905bc041490c963b2b5d3fa993ba7d8631db0ec9`；含 supersedes/纠错溯源、机器导出 11 文件哈希清单、三份生效计划文本磁盘直取 sha256）。
- `postwrite-checks.json` — 11 项写后绑定检查全过。
- `build_corrected_verdict.py` — 可复算的构建/检查脚本。
- 本 CORRECTION.md。

无产品/源码/测试/既有封存记录改动；无 git 操作；无凭据读取；交易边界不变（False/hold/NOT_RUN/0/0）。
