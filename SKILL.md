---
name: schedule-sync
description: 日程同步（Upstash 为唯一权威源）。把事件型日程写入 Upstash Redis，本地 App 定时全量拉取后自行触发提醒。当用户说「加个安排」「记进日程」「提醒我」「改时间」「删掉这条」「今天/这周有什么安排」「查一下云端有没有这条」「志愿活动报名/签到」时使用。
agent_created: true
version: 1
---

# 日程同步

Agent 是唯一写入方，Upstash 是唯一权威数据源，本地 App 只读消费（每 10 分钟全量拉取、本地覆盖）。数据单向流动，不存在双向同步与冲突解决。

本 skill 只管「事件数据的增删改查」。**不做提醒推送、不做周期展开、不做智能去重** —— 这三件事分别归本地 App 和 Agent 自己。

## 铁律

1. **一切安排都是带具体日期的一次性事件**。识别到周期语义（"每周一""每天""每月""每两周"）→ 由你拆成多条独立事件，逐条提交。**禁止**构造 `repeat` / `rrule` / `frequency` / `recurrence` 字段，脚本会直接拒绝（`recurrence_not_supported`）。
2. **先查后提**。每次 `add` 之前，必须先 `query` 目标事件当天（当天 `00:00+08:00` 到次日 `00:00+08:00`）的已有事件，按 `title + time` 逐条比对；已存在的标记 `skipped`，不重复提交。
3. **失败重试前重新 query**。只补交确认缺失的条目，不要整批重提。
4. **删除分级确认**。用户点名删某条 → query 拿到 id 后直接删；用户说"清理这周""把这批都删了"这类模糊/批量指令 → 先列出待删清单（标题 + 时间 + id），用户确认后才执行。
5. **提醒只写相对偏移**。用户说"提前 1 小时" → `remind_offsets: [-60, 0]`；只说"提前提醒"或未提及 → 用默认值（普通事件 `[-25, 0]`，全天事件 `[480, 540]`）；"只在开始前提醒" → `[-25]`。**绝不写绝对提醒时刻**，库里也不存绝对提醒时间。
6. **链接写进 `custom`**。志愿报名/签到、腾讯会议、邮件、取件码等，从对话中提取 URL 或编号写进 `custom`，并在回复里向用户复述已附带的链接。
7. **改事件先定位**。先 `query` 拿到 id 与当前值 → `update` 只传变更字段 → 回复中说明改前改后。
8. **单批 ≤ 20 条**。待提交超过 20 条时自行分批，批间向用户简报进度（如"已提交 20/45"）。
9. **时间一律东八区**。所有 `time` 都带 `+08:00`。用户说"明天下午三点"由你换算成 `2026-10-24T15:00:00+08:00`，不要传相对时间或自然语言。

## 命令

脚本：本 skill 目录下 `scripts/schedule.py`（Python 3 标准库，无第三方依赖）。
下文 `S` 代指该脚本路径；Windows 用 `python`，Linux/macOS 用 `python3`。

| 命令 | 用途 | 关键参数 |
|---|---|---|
| `init` | 首次写入凭证并验证连通性 | `--url` `--token` `--force` |
| `doctor` | 自检：凭证 / 连通性 / 键结构 / 时钟 | — |
| `add` | 批量提交，≤20 条 | `--json` 或 `--file`，也可 stdin |
| `update` | 按 id 部分更新（PATCH） | `--id` + `--json`/`--file` |
| `delete` | 按 id 删除 | `--id` |
| `query` | 查询全部或时间范围 | `--from` `--to`（可选） |
| `pull` | 全量导出（App 用，Agent 一般不需要） | — |

所有命令输出单个 JSON 对象，退出码 0 表示成功、1 表示失败。所有命令都接受 `--no-proxy`（本机代理不通时直连）。

```bash
# 提交
python3 S/scripts/schedule.py add --json '[{"title":"取快递","time":"2026-10-24T14:00:00+08:00","duration":30,"location":"菜鸟驿站","custom":{"pickup_code":"8848"}}]'

# 查当天
python3 S/scripts/schedule.py query --from 2026-10-24 --to 2026-10-25

# 改
python3 S/scripts/schedule.py update --id evt_1729699200000_a3f2k9 --json '{"time":"2026-10-24T16:00:00+08:00"}'

# 删
python3 S/scripts/schedule.py delete --id evt_1729699200000_a3f2k9
```

`query` 的 `--from` 含、`--to` 不含（开区间）；只给日期时按整天算。不传参数即全量。

## 事件字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `title` | **是** | 事件标题，空字符串会被拒（`empty_title`） |
| `time` | **是** | ISO 8601，带 `+08:00`；全天事件写当天 `00:00:00+08:00` |
| `duration` | 否 | 分钟数；不写 = 瞬时事件；全天事件固定 `1439`（脚本自动设） |
| `all_day` | 否 | 默认 `false`。设 `true` 时脚本自动把 `time` 归零到当天 00:00、`duration` 设 1439、提醒默认 `[480,540]` |
| `location` | 否 | 地点 |
| `note` | 否 | 备注 |
| `remind_offsets` | 否 | 相对事件 `time` 的分钟偏移数组，可为负。默认普通 `[-25,0]` / 全天 `[480,540]` |
| `custom` | 否 | 开放字典，任意键值原样透传。常用：`pickup_code` `signup_url` `meeting_url` `meeting_id` `mail_link` |

`id` / `created_at` / `updated_at` / `schema_version` 由脚本生成与维护，提交时不要带；`update` 里传会被拒（`immutable_field`）。

字段细节、存储结构、App 侧契约见 `references/data-model.md`。

## 标准流程

### 新增一条

1. 从对话里抽取：标题、日期时间、时长、地点、备注、链接、提醒要求。缺时长可问一句"大概多久"（用户不在意就按瞬时事件处理，不写 `duration`）。
2. `query --from <当天> --to <次日>`，比对 `title + time`。
3. 未存在 → `add`；已存在 → 告诉用户"这条已经在日程里了"，不重复写。
4. 回复时复述：标题、时间、地点、已附带的链接、提醒档位。

### 批量 / 周期性

"每周一三五早八的课，到 12 月底" → 自己展开成一条条具体日期事件，每批 ≤20 条提交，批间报进度。展开后核对条数与日期范围是否覆盖完整。

### 修改

先 `query` 定位（可带时间范围缩小），拿到 `id`，再 `update` 只传变更字段。改提醒用 `remind_offsets`，改时间只传 `time`。

### 删除

- 明确指名："删掉明天下午的取快递" → `query` 定位 → `delete --id`。
- 模糊批量："把这周的会都清掉" → `query` 出清单 → 列给用户确认 → 逐条 `delete`。

### 查询回答

`query` 拿原始事件数组后由你做语义过滤和表达。回答时按时间升序，标题、时间、地点齐全；有链接的一并给出。

## 凭证

- 路径固定：`~/.config/upstash-schedule/credentials.json`（Windows 即 `C:\Users\<用户名>\.config\upstash-schedule\credentials.json`）。
- 首次使用：`doctor` 或任意命令报 `credentials_missing` → 在对话里向用户索取 **Upstash REST URL** 和 **REST Token**（Upstash 控制台 → 选中数据库 → REST API 区域复制），然后：

```bash
python3 S/scripts/schedule.py init --url "https://xxx.upstash.io" --token "<token>"
```

- 凭证只存在本地文件（权限 600），脚本代码与文档内不含任何凭证，日志不打印 token。
- 用户想换库：`init --force` 覆盖。

## 自检与排错

先跑 `doctor`，四项结果一目了然：凭证、连通性（PING）、键结构（`schedule:events` 为 hash、`schedule:timeline` 为 zset）、本机时钟与服务器偏差。

| 现象 | 处理 |
|---|---|
| `credentials_missing` | 走上面的 init 流程 |
| `network_error` / 连接超时 | 加 `--no-proxy` 重试 |
| `auth_failed` (401) | token 错，重新索取 |
| `forbidden` (403) | token 是只读的，需要可读写 REST token |
| `recurrence_not_supported` | 自己拆成多条一次性事件 |
| `batch_too_large` | 分批到 ≤20 条 |
| 用户说"看不到某条" | `query` 该时间范围核实，确认 `time` 与标题，再解释；不要只说"已写入" |

## 明确不做

| 不做 | 归属 |
|---|---|
| 提醒推送 / 通知 | 本地 App |
| 周期事件展开 | Agent（本 skill 的调用方） |
| 智能去重 | Agent 先查后提；脚本只拒绝同批内的完全重复 |
| 保存凭证以外的任何密钥 | 只有路径常量 |
| 软删除 / 墓碑 | 不需要，App 全量覆盖天然同步删除 |
| 双向同步 | 不需要，单向权威源 |
