# schedule-sync

把日程事件写入 [Upstash Redis](https://upstash.com/)，本地客户端定时全量拉取后自行触发提醒。**Agent 是唯一写入方，Upstash 是唯一权威源，本地 App 只读消费**——数据单向流动，不做双向同步，也就没有冲突解决。

这是 [ima.copilot](https://ima.qq.com) 的 skill：`SKILL.md` 是给 Agent 读的执行说明，`scripts/schedule.py` 是实际干活的 CLI（纯 Python 3 标准库，无第三方依赖）。

## 特点

- **零依赖**：只用 Python 3 标准库，拷走就能跑（Windows / macOS / Linux）
- **单向数据流**：不存在双向同步与冲突，删了就是删了（App 全量覆盖天然同步删除）
- **只存相对提醒**：库里只存 `remind_offsets`（如 `[-25, 0]` = 提前 25 分钟 + 开始时刻），不存绝对提醒时刻，换时区/改时间都不用重算
- **拒绝隐式周期**：`repeat` / `rrule` / `frequency` / `recurrence` 字段会被脚本直接拒绝，周期性安排必须由调用方展开成一条条具体日期的事件
- **凭证本地化**：REST URL 与 Token 只存在本机 `~/.config/upstash-schedule/credentials.json`（权限 600），代码与文档内不含任何凭证

## 数据结构

| 键 | 类型 | 用途 |
|---|---|---|
| `schedule:events` | Hash | field = 事件 id，value = 事件 JSON |
| `schedule:timeline` | ZSet | score = 事件时间戳，member = 事件 id，用于按时间范围查询 |

字段定义与 App 侧契约见 [`references/data-model.md`](references/data-model.md)。

## 快速开始

```bash
# 1) 写入凭证（Upstash 控制台 → 选中数据库 → REST API 区域复制 URL 与 Token）
python3 scripts/schedule.py init --url "https://xxx.upstash.io" --token "<REST_TOKEN>"

# 2) 自检：凭证 / 连通性 / 键结构 / 时钟偏差
python3 scripts/schedule.py doctor

# 3) 提交一条事件
python3 scripts/schedule.py add --json '[{"title":"取快递","time":"2026-10-24T14:00:00+08:00","duration":30,"location":"菜鸟驿站","custom":{"pickup_code":"8848"}}]'

# 4) 查当天
python3 scripts/schedule.py query --from 2026-10-24 --to 2026-10-25

# 5) 改 / 删（先 query 拿 id）
python3 scripts/schedule.py update --id evt_1729699200000_a3f2k9 --json '{"time":"2026-10-24T16:00:00+08:00"}'
python3 scripts/schedule.py delete --id evt_1729699200000_a3f2k9
```

## 命令

| 命令 | 用途 | 关键参数 |
|---|---|---|
| `init` | 首次写入凭证并验证连通性 | `--url` `--token` `--force` |
| `doctor` | 自检：凭证 / 连通性 / 键结构 / 时钟 | — |
| `add` | 批量提交，≤20 条 | `--json` 或 `--file`，也可 stdin |
| `update` | 按 id 部分更新（PATCH） | `--id` + `--json`/`--file` |
| `delete` | 按 id 删除 | `--id` |
| `query` | 查询全部或时间范围 | `--from` `--to`（可选） |
| `pull` | 全量导出（App 用） | — |

所有命令输出单个 JSON 对象，退出码 `0` 成功 / `1` 失败；都接受 `--no-proxy`（本机代理不通时直连）。

`query` 的 `--from` 含、`--to` 不含；只给日期时按整天算；不传参数即全量。

## 事件字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `title` | 是 | 事件标题，空字符串会被拒 |
| `time` | 是 | ISO 8601 带 `+08:00`；全天事件写当天 `00:00:00+08:00` |
| `duration` | 否 | 分钟；不写 = 瞬时事件；全天事件固定 `1439` |
| `all_day` | 否 | `true` 时自动归零时间、`duration=1439`、提醒默认 `[480,540]` |
| `location` | 否 | 地点 |
| `note` | 否 | 备注 |
| `remind_offsets` | 否 | 相对 `time` 的分钟偏移，默认普通 `[-25,0]` / 全天 `[480,540]` |
| `custom` | 否 | 开放字典，原样透传（`pickup_code` / `signup_url` / `meeting_url` / `meeting_id` / `mail_link` …）|

`id` / `created_at` / `updated_at` / `schema_version` 由脚本生成，提交时不要带。

## 作为 skill 使用

把整个目录放进 skills 目录即可，Agent 会读 `SKILL.md` 按里面的铁律执行：

```
skills/schedule-sync/
├── SKILL.md
├── scripts/schedule.py
└── references/data-model.md
```

`SKILL.md` 里的核心约束：一切安排都是带具体日期的一次性事件（周期语义自己展开）、每次 `add` 前先 `query` 比对避免重复、删除分级确认、提醒只写相对偏移、单批 ≤20 条、时间一律东八区。

## 排错

| 现象 | 处理 |
|---|---|
| `credentials_missing` | 走 `init` 流程 |
| `network_error` / 超时 | 加 `--no-proxy` 重试 |
| `auth_failed` (401) | token 错，重新复制 |
| `forbidden` (403) | token 只读，需要可读写 REST token |
| `recurrence_not_supported` | 自己拆成多条一次性事件 |
| `batch_too_large` | 分批到 ≤20 条 |

## 许可

私人使用。仓库默认私有，请勿公开自己的 Upstash Token。
