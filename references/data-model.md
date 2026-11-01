# 数据模型与契约

## 事件对象

```json
{
  "schema_version": 1,
  "id": "evt_1729699200000_a3f2k9",
  "title": "取快递",
  "time": "2026-10-24T14:00:00+08:00",
  "duration": 30,
  "all_day": false,
  "location": "菜鸟驿站",
  "note": "凭取件码取件",
  "remind_offsets": [-25, 0],
  "custom": {"pickup_code": "8848"},
  "created_at": "2026-10-23T11:00:00+08:00",
  "updated_at": "2026-10-23T11:00:00+08:00"
}
```

| 字段 | 类型 | 写入方 | 说明 |
|---|---|---|---|
| `schema_version` | int | 脚本 | 当前固定 `1`，供 App 解析兼容 |
| `id` | string | 脚本 | `evt_{毫秒时间戳}_{6位随机小写字母数字}`，提交后返回给 Agent |
| `title` | string | Agent | 必填，去首尾空白后不能为空 |
| `time` | string | Agent | 必填，ISO 8601；无时区按 `+08:00` 处理，其他时区自动换算 |
| `duration` | int | Agent | 分钟，`>=0`；不写 = 瞬时事件；`all_day=true` 时强制 `1439` |
| `all_day` | bool | Agent | 默认 `false`；为 `true` 时 `time` 归零到当天 00:00 |
| `location` | string | Agent | 可空，空值不落库 |
| `note` | string | Agent | 可空，空值不落库 |
| `remind_offsets` | int[] | Agent | 相对 `time` 的分钟偏移，可为负、可为空数组（= 不提醒） |
| `custom` | object | Agent | 完全开放，原样透传；约定键见下 |
| `created_at` | string | 脚本 | 创建时刻，`update` 时不变 |
| `updated_at` | string | 脚本 | 每次 `update` 刷新 |

`custom` 约定键（不强制，仅便于 App 与 Agent 对齐）：

| key | 场景 |
|---|---|
| `pickup_code` | 快递取件码 |
| `signup_url` | 志愿活动 / 活动报名链接 |
| `meeting_url` / `meeting_id` | 线上会议 |
| `mail_link` | 邮件链接 |

## 提醒模型

库里**不存任何绝对提醒时间**，只存相对 `time` 的分钟偏移。App 读到事件后自行计算绝对时刻。

| 场景 | 默认 `remind_offsets` | 含义 |
|---|---|---|
| 普通事件 | `[-25, 0]` | 开始前 25 分钟 + 开始时刻 |
| 全天事件 | `[480, 540]` | 当天 08:00 + 09:00 |

用户指定提醒时按指定值写入，例如"提前 1 小时"→ `[-60, 0]`，"只在开始前提醒"→ `[-25]`。

## 存储结构

```
schedule:events      Hash    field = 事件 id，value = 事件完整 JSON（主存储）
schedule:timeline    ZSET    score = time 的毫秒时间戳，member = 事件 id（时间索引）
```

- 写入 / 修改 / 删除都通过 Lua 脚本一次执行，保证 Hash 与 ZSET 原子一致；`update` 改时间时同步重设 score。
- 时间范围查询：`ZRANGEBYSCORE schedule:timeline <from> (<to)` 取 id → `HMGET schedule:events` 取详情。
- 全量：`HGETALL schedule:events`，按 `time` 升序输出。
- 删除是物理删除（`HDEL` + `ZREM`），没有墓碑、没有软删除。

## pull 契约（App 侧）

`pull` 输出：

```json
{
  "ok": true,
  "command": "pull",
  "schema_version": 1,
  "generated_at": "2026-10-23T12:00:00+08:00",
  "count": 42,
  "events": [ ... ]
}
```

App 消费规则：

1. 每 10 分钟拉一次，用返回的 `events` **整体覆盖**本地日程。
2. 云端删除的事件在全量覆盖后自然消失，App 不需要处理删除标记。
3. 提醒由 App 在本地按 `remind_offsets` 计算并触发；同步到已过提醒点的，不补发。
4. 已过期事件仍会出现在 `events` 里，由 App 自行决定是否展示。

## 校验与错误码

单条校验失败只影响该条，同批其他条照常处理；批量超限则整批拒绝。

| 错误码 | 触发条件 |
|---|---|
| `empty_title` | `title` 缺失或空白 |
| `invalid_time` | `time` 缺失或不是合法 ISO 8601 |
| `recurrence_not_supported` | 出现 `repeat` / `rrule` / `frequency` / `recurrence` / `recurrence_rule` / `repeat_rule` / `repeats` |
| `invalid_duration` | `duration` 非整数或为负 |
| `invalid_remind_offsets` | 不是整数数组 |
| `invalid_custom` | `custom` 不是对象 |
| `batch_too_large` | 单批超过 20 条 |
| `empty_batch` | 提交空数组 |
| `invalid_payload` | 输入不是合法 JSON 或结构不对 |
| `not_found` | 指定 id 不存在（`update` / `delete`） |
| `immutable_field` | 试图修改 `id` / `created_at` / `updated_at` / `schema_version` |
| `credentials_missing` | 凭证文件不存在 |
| `credentials_invalid` | 凭证文件损坏或缺字段 |
| `credentials_exists` | 已有凭证且未加 `--force` |
| `network_error` | 连不上 Upstash（可加 `--no-proxy`） |
| `auth_failed` / `forbidden` | token 错误 / 权限不足 |

`add` 的每条结果状态：`ok`（已入库，带 `id`）、`skipped`（本批内 `title+time` 完全重复）、`error`（带 `error` 与 `reason`）。

## 本地文件

| 路径 | 内容 |
|---|---|
| `~/.config/upstash-schedule/credentials.json` | `{"url": "...", "token": "..."}`，权限 600 |
| `~/.config/upstash-schedule/logs/YYYY-MM-DD.jsonl` | 每次 `add` / `update` / `delete` 的时间、命令、事件 id、结果；只保留最近 3 天 |
