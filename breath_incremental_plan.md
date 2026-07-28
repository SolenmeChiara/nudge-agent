# breath 完整展示 + context 记忆段增量渲染（2026-07-28 施工档案）

Sol 批准口径（7/28 上午，终端插话原话）：预算吞段这个，可以让记忆条目完整展示，
但完整展示的话 context.md 得做和查看 session 一样的去重加增量更新，否则每次
看 md 都会重复看到相同的东西耗 token。

## 现状（侦察结论，file:line 均已核实）

### memory 侧（D:/ClaudeExtentions/MCP/Sol-Memory-mcp/memory_mcp.py，4410 行）
- `_compose_breath_output`（429-608）三条路径共用：extmcp_breath 工具（3204-3217，do_touch=True）、
  GET /breath-hook（3410-3425，do_touch=False）、CLI 子命令（4323-4335）
- `BREATH_TOKEN_BUDGET = 3000`（412，env 可覆盖）字符预算
- 截断在内嵌 `_emit_segment`（537-555）：header 超预算整段 return（543），行超预算 break（551）
  ——「标题在内容空」吞段根因；条目本身全文渲染（`_fmt` 526-531，不截）
- 行格式：`[id:mem_xxx] [weight:… V…/A…] key: 全文`；段=== PINNED/CORE/WORKING/WATCH/TOP UNRESOLVED ===
- scent 彩蛋行（603-606）无 id 前缀，纯氛围，附在文末
- TOP UNRESOLVED 每次随机轮换采样（517-523），CORE 按日轮换 2 条——真正跨班重复的是
  PINNED（固定 2 条）、WORKING、WATCH

### 注入器侧（/mnt/d/ClaudeExtentions/MCP/nudge-agent/nudge_inject.py，970 行，Windows 常驻）
- breath 段：`_try_breath_hook`（247-257）GET 原文粘贴进 context（312-315）
- 48h 段：`_build_memory_block`（275+）直接 ro 只读查库，`_fmt_rows`（260-272）每条截 120 字
- 对话增量 state 可复用范式：`context_state.json`（uuid→updated_at 平面 dict），
  比对在 fetch_context.fetch_raw（289-315），state 读写有 try/except 兜底（226-241），
  注入器启动时删 state 文件保首班全量（887-893），省略条目归入段尾统计行（format_block:372）

## 设计

### memory 侧改动（小）
1. `_compose_breath_output` 加参数 `budget: int | None = None`，默认落到 BREATH_TOKEN_BUDGET；
   `_emit_segment` 在 budget is None 时不做任何截断
2. /breath-hook 端点传 budget=None → 完整输出（吞段根治点）
3. extmcp_breath 工具路径与 CLI 不传 → 维持 3000，前台 claude.ai 上下文成本不变

### 注入器侧改动（核心）
1. 新 state 文件 `memory_state.json`（与 context_state.json 并列，同样启动即删、写失败不破管线）
   结构：`{"breath": {mem_id: line_hash}, "recent48h": {row_key: content_hash}}`
2. breath 段增量：按行解析 hook 返回文本——
   - `=== X ===` 段头保留；`[id:mem_xxx]` 行以 id 为键、整行 sha1 为变更判断
   - 无 id 前缀的行（scent、WORKING (n/m) 头等）原样保留
   - **WORKING/WATCH 段例外：永远全文**（活跃层+危机档案，条数少，省略风险大于收益）
   - 其余段：id 在 state 且 hash 未变 → 整条省略；段尾统计
     「（另有 N 条高权重记忆自上次 context 后未变，已省略；需要全文用 extmcp_get_memory 按 id 拉）」
3. 48h 段增量+去截断：`_fmt_rows` 的 120 字截断去掉（完整展示）；
   row_key 用 `created_at|key` 的 sha1，content 的 sha1 做变更判断；
   未变条目整条省略+段尾统计行（同上范式，注明可用 extmcp_search_memory / get_memory 拉）
4. 重启删 state：在 887-893 现有 unlink 处并列加一行

### 不动的
- 前台 breath 工具行为、touch/cooldown 逻辑、TOP UNRESOLVED 采样、CORE 轮换、scent
- BREATH_TOKEN_BUDGET 常量本身保留（工具路径继续用）

## 验证清单
- 离线：ast.parse + ruff 两文件
- 行为（临时实例，不碰生产）：memory_mcp.py 起临时端口+临时库，
  curl /breath-hook 确认完整输出且工具路径仍截 3000；
  注入器侧函数抽出可单测：喂两次相同 breath 文本 → 第二次省略+统计行；
  改一条 WORKING → WORKING 永远全文；state 文件损坏 → 全量渲染不炸
- 部署（需 Sol / 权限门）：重启 Windows 侧 memory HTTP 服务 + 注入器；
  重启后首班应全量，次班出现省略统计行

## 进度
- [x] 侦察（本档案，2026-07-28 09:0x 班）
- [x] 实现（opus 子代理，测试三层全实跑；吞段 bug 在临时库复现后修复，端到端增量省 63%）
- [x] 审查（独立 opus，PASS_WITH_NOTES；三处修正已并入：hash 剥 weight——decay 连续漂移会
      永久击穿折叠、48h 省略行列 key、memory 侧 _fmt flatten key。前台 extmcp_breath 仍吞段
      属计划内取舍，已告知 Sol）
- [x] 提交（nudge-agent 20d1b8a feat + docs；Sol-Memory-mcp 4ae0a37。
      start_tmux_agent.bat 一行 flag 被分类器拦提交，工作树保留，待 Sol 处理）
- [x] 部署（Sol 2026-07-28 执行：memory 服务 10:53:45 起 → 注入器 10:54:08 起，顺序正确同一分钟内）
- [x] 部署验证 10:54 班完成 ①③⑤ + 单例锁：
      ①首班 cycle #1 两 state 清空、全量渲染 21489 chars、memory_state.json 10:54 生成 ✓
      ③记忆段无「标题在内容空」✓
      ⑤extmcp_breath 直调 3000 量级正常（WATCH/TOP 空是 6h 激活冷却——注入器刚呼吸过，非吞段）✓
      48765 单实例（PID 7376）✓
- [x] 余项 11:35 班验收：②次班省略统计行全部出现（48h 段整条省略带 key 索引、PINNED/CORE
      折叠成 id 行、TOP 尾部统计行），WORKING/WATCH 仍全文，无空段 ✓
      ④稳态 11710 chars vs 首班 21489（-45%），机制生效 ✓。**部署验收全部关账。**
      另待 Sol：start_tmux_agent.bat 那行 flag 仍是未提交的 M 状态。
      follow-up 两条（embedding timeout / 冷启动记档）排下周额度。
