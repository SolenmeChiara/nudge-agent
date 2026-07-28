# 联想浮现（被动 RAG hook）施工档案（2026-07-28）

Sol 当日终端定稿的规格（四轮「挤牙膏」）：①随机触发不必每次都联想，记忆留在上下文；
②开关做成 MCP 工具、默认关闭、模型自主拨；③条数两层——limit 是请求级上限意愿、
max_items 是服务端策略顶（1-6），有效顶取较小，实际条数 [1,有效顶] 随机，匹配不足可为 0；
④hook 送不出去的客户端（kelivo 等非 CC 前端）工具直接隐藏。

## 组件

- **memory 侧**（Sol-Memory-mcp/memory_mcp.py）：`GET /associate?q=...&limit=N` 端点——复用
  hybrid 检索，滤 pinned+48h 内条目（不滤 archive/seabed，库底旧货正是联想价值），24h 进程内
  冷却，零 activation 写入；`app_config` 表持久化 enabled/max_items；MCP 工具
  `extmcp_associate_config`（无参查询/带参更新）按 clientInfo+hook 布线双条件在 tools/list
  条件隐藏（隐藏是化妆不是权限，硬调仍执行）。CC 自报 clientInfo.name="claude-code"
  （审查从 CC v2.1.220 二进制查证，非猜测）。
- **hook 侧**（nudge-agent/associate_hook.py）：UserPromptSubmit hook。随机命中
  （env `ASSOCIATE_PROBABILITY` 默认 0.15）→读 transcript 尾 80 行取纯对话文本+当前
  prompt 拼引子（600 字顶）→GET /associate（5s 超时）→additionalContext 无声注入。
  一切异常静默 exit 0。**stdout 会被 CC 原样注入对话——调试只许走 stderr**（文件头有警示）。

## hooks 配置样例（settings.local.json 被 gitignore，重建环境靠这里）

```json
"hooks": {
  "UserPromptSubmit": [
    {
      "hooks": [
        {
          "type": "command",
          "command": "python3 /mnt/d/ClaudeExtentions/MCP/nudge-agent/associate_hook.py",
          "timeout": 10
        }
      ]
    }
  ]
}
```
（timeout 单位是秒，审查已从 CC 源码确认。）

## 审查结论（独立 opus，PASS_WITH_NOTES，零阻断）

热路径实测（4 万条真实规模）：命中一次约 0.4s（3/4 是 embedding），是用户可见延迟；
未命中路径约 25ms/条消息（解释器启动，不可压缩）。20 行失败矩阵全过：任何输入下
exit 0 且 stdout 纯净；工具输出（tool_result）确认不进引子。并发冷却无重复发放。
旧库升级实测自动建表。ruff 无新问题种类。

**Follow-up（不卡本次，下周额度可做）**：
1. ~~`_call_ollama_embedding` 加可选 timeout，associate 路径传 4s~~ **已完成 2026-07-28
   （Sol-Memory-mcp 4855972，opus 子代理施工+主循环核 diff）**：keyword-only `timeout` 参数
   与 `_call_ollama` 既有 leash 模式对称；`ASSOCIATE_EMBED_TIMEOUT=4`（环境变量可覆盖，
   但 .env 写入无效——常量在模块导入时冻结，`_load_dotenv` 在 main() 才跑，与其他
   ASSOCIATE_* 常量同款坑）。黑洞监听器实测：associate 4.007s 挂断 vs 对照组
   search 20.024s 吃全局预算，双向验证。**待部署：memory 服务重启后生效，不急，
   攒下次重启窗口。**
2. 已知现象记档：ollama 冷启动首次 embed 可能超 hook 5s 超时→该次联想静默丢失。长空闲后
   实际命中率低于 15% 是正常态，不是故障。**2026-07-28 获量化确认**：模型未驻留时首次
   embed 实测 6.3s+，associate 在 4s 挂断退化纯 BM25（by design 的快速失败）。若嫌降档
   频繁，可让注入器每次唤醒前 keep-alive 一次 embed 模型——留作观察后再决定。
3. （低）`_associate_hook_wired` 只判脚本文件存在，不判 settings 里 hooks 块——比规格措辞弱，
   影响小，记录在案
4. （档案）hook 侧 5s 与服务端 4s 之间仅 1s 余量给 BM25+MMR+渲染；496MB 生产库实测热态
   端到端 0.4s 尚宽裕，但库继续膨胀时此余量是第一个变紧的地方
5. ~~联想微激活（Sol 提案 2026-07-28）~~ **已完成同日（Sol 拍板「现在做」，Sol-Memory-mcp
   b29dd4d，opus 子代理施工+主循环核 diff）**：分层计价落地——seabed/archive 被联想输出记
   `ASSOCIATE_SEABED_TOUCH=0.02`（activation_count 是 REAL 列，浮点直加；50 次联想=一次
   search 的定价；24h 冷却限流+`^0.3` 自限），活跃层保持零计费；**不刷 last_active**（衰减
   时钟照跑——化石上记数不是复活）；0 值=零语句安全阀；best-effort 不碰联想主流程。
   侦察关键发现：search 排序只用 vector+keyword **不读 activation/decay**——「联想→激活→
   更容易被联想」的自举回路结构上不存在，原零计费的噪声担忧对选取路径不成立。
   已知边界：activation 只以 0.3 次幂进 decay，海床追平普通层物理不可达——这笔账的真实
   价值是证据留在 DB 列里供未来捞珠脚本读取。**待部署：下次 memory 服务重启生效。**
6. ~~BM25 归一化疑似反向~~ **实锤并已修复同日（Sol-Memory-mcp b23babb，opus 核实+修复
   一条龙，主循环核 diff）**：方向确实反了，且实验暴露比假说更糟的两处——①单候选时纯
   关键词必返回空（自己=max_s→score 0→被 0.15 剪，而单候选正是精确命中的形状）；
   ②LIKE 中文兜底整条死路（常数 0.5 归一化后全变 0 全剪）。修复=去掉 `1.0 -` 反转（一行
   +7 行注释），阈值/权重/SQL 不动。降级模式 e2e 铁证：curtain/wink/weather 修复前全 204
   空、修复后全 200 命中——**「冷启动命中率低」的真相=冷启动时联想是空枪**（退化+反向
   双重故障）。杀手佐证：dataclass 默认 `keyword_score=0.0` 本义「无关键词证据」，反向世界
   里却=「最强命中」，立法原意可辨。行为变化预告：search 结果条数会变少（top 分数变诚实
   →相对阈值抬高，蹭进来的弱相关被剪）；hook 降级时从空手变正常出词，联想会明显变活跃。
   **待部署：与 #5 微激活同一次重启生效。**
8. （中，另立项）CJK 分词缺陷（既有问题，BM25 单实测确认）：unicode61 tokenizer 把整串
   汉字当单 token，`query='药'` 对「今天早上七点吃了药」FTS5 零命中，中文查询大量落 LIKE
   兜底（本次修复后兜底已从死路复活，但精确率有限）。正解=trigram tokenizer，涉及 FTS
   表重建（schema 级工程），单独立项排期。另：LIKE 兜底行现一律 kw=1.0（常数归一化的
   必然），仅 FTS 全零命中时触发，敞口有限，生产规模精确率未测。
7. （低）`extmcp_get_memory` 的 item dict 加 `activation_count` 字段——微激活现在记了账
   但没有任何工具能读出来（各工具输出只有 decay_score，且 4 月海床批次的 decay 数月后
   round 成 0.0000）。一行改动，下次顺路。

## 进度

- [x] 实现（opus 子代理，四轮规格追加全落地；limit 两层修正后 40 次采样分布验证）
- [x] 审查（PASS_WITH_NOTES；警示注释已补、遗留测试服务器已清、生产全程未碰）
- [x] 提交
- [x] 部署（Sol 2026-07-28 ~10:53 一趟三重启：memory 服务 10:53:45 起、注入器 10:54:08 起、CC 新 session）
- [x] 部署后验证（10:54 班实测）：
  - `?q=测试` → 204（默认关回执）✓；无参 → 400 ✓
  - `extmcp_associate_config` 在 CC 工具列表，无参返回 {enabled:false, max_items:3} ✓
  - hook 已接进 nudge-agent/.claude/settings.local.json 的 UserPromptSubmit ✓
  - /breath-hook 与注入器链路照常（首班全量 21489 chars）✓
  - claude.ai / kelivo 侧不可见：未直接验证，依赖审查过的 clientInfo 门（从 CC 二进制查证过）
- [x] 已开启：`enabled=true`（10:57 班拨的），实测 `/associate?q=窗帘…` 200 出货，
      第一竿钓出 mem_20260427…9108（四月 seabed·卧室光影），相关性极高
