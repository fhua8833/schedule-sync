#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Upstash 日程同步 CLI —— 事件型日程的唯一写入方。

Agent 通过本脚本把日程写入 Upstash Redis；本地 App 定时 pull 全量后自行触发提醒。
本脚本不含任何推送、通知、周期展开、去重判断逻辑。

命令：init / doctor / add / update / delete / query / pull
约定：每个命令向 stdout 输出一个 JSON 对象；退出码 0 成功，1 失败。
依赖：Python 3.8+ 标准库，无第三方包，Windows / Linux / macOS 通用。
"""

import argparse
import json
import os
import re
import secrets
import string
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# --------------------------------------------------------------------- 常量

SCHEMA_VERSION = 1
MAX_BATCH = 20
LOG_KEEP_DAYS = 3
TIMEOUT = 25

EVENTS_KEY = "schedule:events"
TIMELINE_KEY = "schedule:timeline"

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "upstash-schedule")
CRED_FILE = os.path.join(CONFIG_DIR, "credentials.json")
LOG_DIR = os.path.join(CONFIG_DIR, "logs")

TZ = timezone(timedelta(hours=8))

DEFAULT_REMIND = [-25, 0]
DEFAULT_REMIND_ALL_DAY = [480, 540]
ALL_DAY_DURATION = 1439

RECURRENCE_FIELDS = (
    "repeat", "rrule", "frequency", "recurrence",
    "recurrence_rule", "repeat_rule", "repeats",
)
IMMUTABLE_FIELDS = ("id", "schema_version", "created_at", "updated_at")
ALPHABET = string.ascii_lowercase + string.digits

# 两个存储键必须原子同步：Hash 存事件、ZSET 存时间索引
LUA_ADD = """
local n = 0
for i = 1, #ARGV, 3 do
  redis.call('HSET', KEYS[1], ARGV[i], ARGV[i+1])
  redis.call('ZADD', KEYS[2], tonumber(ARGV[i+2]), ARGV[i])
  n = n + 1
end
return n
"""

LUA_UPDATE = """
if redis.call('HEXISTS', KEYS[1], ARGV[1]) == 0 then
  return 0
end
redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
redis.call('ZADD', KEYS[2], tonumber(ARGV[3]), ARGV[1])
return 1
"""

LUA_DELETE = """
local n = redis.call('HDEL', KEYS[1], ARGV[1])
redis.call('ZREM', KEYS[2], ARGV[1])
return n
"""


# --------------------------------------------------------------------- 错误

class CliError(Exception):
    """带错误码的业务异常，直接转成 JSON 输出。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def emit(payload):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    sys.stdout.flush()


# --------------------------------------------------------------------- 时间

def now_tz():
    return datetime.now(TZ)


def iso(dt):
    return dt.isoformat(timespec="seconds")


def iso_now():
    return iso(now_tz())


def parse_time(value, field="time"):
    """解析 ISO 8601，统一换算成 +08:00。无时区按东八区处理。"""
    if not isinstance(value, str) or not value.strip():
        raise CliError("invalid_time", "%s 必填，且必须是 ISO 8601 字符串" % field)
    s = value.strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise CliError("invalid_time", "%s 不是合法的 ISO 8601：%r" % (field, value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def to_ms(dt):
    return int(dt.timestamp() * 1000)


def is_date_only(value):
    return isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()) is not None


# --------------------------------------------------------------------- 校验

def _as_int(value, field):
    if isinstance(value, bool):
        raise CliError("invalid_%s" % field, "%s 必须是整数，收到布尔值" % field)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip())
    raise CliError("invalid_%s" % field, "%s 必须是整数，收到 %r" % (field, value))


def _as_text(value, field):
    if value is None:
        return None
    if not isinstance(value, str):
        raise CliError("invalid_%s" % field, "%s 必须是字符串，收到 %r" % (field, value))
    value = value.strip()
    return value or None


def normalize_event(raw):
    """校验并规范化一条事件，返回字段已补齐的 dict（可选字段可能为 None）。"""
    if not isinstance(raw, dict):
        raise CliError("invalid_event", "事件必须是 JSON 对象")

    for field in RECURRENCE_FIELDS:
        if field in raw:
            raise CliError(
                "recurrence_not_supported",
                "不支持周期字段 %s：请拆成多条带具体日期的一次性事件后分批提交" % field,
            )

    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        raise CliError("empty_title", "title 必填且不能为空字符串")

    dt = parse_time(raw.get("time"))

    all_day = raw.get("all_day", False)
    if all_day is None:
        all_day = False
    if not isinstance(all_day, bool):
        raise CliError("invalid_all_day", "all_day 必须是布尔值")

    if all_day:
        dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        duration = ALL_DAY_DURATION
    else:
        if raw.get("duration") is None:
            duration = None
        else:
            duration = _as_int(raw.get("duration"), "duration")
            if duration < 0:
                raise CliError("invalid_duration", "duration 不能为负数")

    offsets = raw.get("remind_offsets")
    if offsets is None:
        offsets = list(DEFAULT_REMIND_ALL_DAY if all_day else DEFAULT_REMIND)
    else:
        if not isinstance(offsets, list):
            raise CliError("invalid_remind_offsets", "remind_offsets 必须是分钟整数数组")
        cleaned = []
        for item in offsets:
            cleaned.append(_as_int(item, "remind_offsets"))
        offsets = cleaned

    custom = raw.get("custom")
    if custom is None:
        custom = {}
    if not isinstance(custom, dict):
        raise CliError("invalid_custom", "custom 必须是 JSON 对象")

    return {
        "title": title.strip(),
        "time": iso(dt),
        "duration": duration,
        "all_day": all_day,
        "location": _as_text(raw.get("location"), "location"),
        "note": _as_text(raw.get("note"), "note"),
        "remind_offsets": offsets,
        "custom": custom,
    }


def clean(norm):
    """去掉值为 None 的可选字段，避免写入无意义的 null。"""
    return {k: v for k, v in norm.items() if v is not None}


def new_id():
    return "evt_%d_%s" % (to_ms(now_tz()), "".join(secrets.choice(ALPHABET) for _ in range(6)))


def build_event(norm):
    stamp = iso_now()
    event = {"schema_version": SCHEMA_VERSION, "id": new_id()}
    event.update(clean(norm))
    event["created_at"] = stamp
    event["updated_at"] = stamp
    return event


# --------------------------------------------------------------------- 凭证与网络

class Client:
    """Upstash REST 客户端。凭证只从凭证文件读，代码内不含任何硬编码。"""

    def __init__(self, url, token, no_proxy=False, timeout=TIMEOUT):
        self.url = url
        self.token = token
        self.timeout = timeout
        handler = urllib.request.ProxyHandler({}) if no_proxy else urllib.request.ProxyHandler()
        self.opener = urllib.request.build_opener(handler)

    def _post(self, path, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url + path,
            data=body,
            method="POST",
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
            },
        )
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace").strip()[:300]
            if e.code == 401:
                raise CliError("auth_failed", "凭证被拒绝（401），请核对 token：%s" % detail)
            if e.code == 403:
                raise CliError("forbidden", "token 权限不足（403），需要可读写的 REST token：%s" % detail)
            raise CliError("http_%d" % e.code, "Upstash 返回 HTTP %d：%s" % (e.code, detail))
        except urllib.error.URLError as e:
            raise CliError(
                "network_error",
                "无法连接 Upstash：%s（本机代理不通时可加 --no-proxy）" % e.reason,
            )
        except OSError as e:
            raise CliError("network_error", "网络异常：%s" % e)

        try:
            return json.loads(raw)
        except ValueError:
            raise CliError("bad_response", "响应不是 JSON：%s" % raw[:200])

    def cmd(self, *args):
        resp = self._post("", [str(a) for a in args])
        if isinstance(resp, dict) and resp.get("error"):
            raise CliError("upstash_error", str(resp["error"]))
        return resp.get("result") if isinstance(resp, dict) else None

    # -- 存储操作 --

    def write_events(self, events):
        flat = []
        for ev in events:
            flat.extend([ev["id"], dumps(ev), str(to_ms(parse_time(ev["time"])))])
        if not flat:
            return 0
        n = self.cmd("EVAL", LUA_ADD, "2", EVENTS_KEY, TIMELINE_KEY, *flat)
        return int(n or 0)

    def replace_event(self, event):
        n = self.cmd(
            "EVAL", LUA_UPDATE, "2", EVENTS_KEY, TIMELINE_KEY,
            event["id"], dumps(event), str(to_ms(parse_time(event["time"]))),
        )
        return int(n or 0)

    def remove_event(self, event_id):
        n = self.cmd("EVAL", LUA_DELETE, "2", EVENTS_KEY, TIMELINE_KEY, event_id)
        return int(n or 0)

    def get_event(self, event_id):
        raw = self.cmd("HGET", EVENTS_KEY, event_id)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def all_events(self):
        raw = self.cmd("HGETALL", EVENTS_KEY)
        pairs = _pairs(raw)
        out = []
        for _, value in pairs:
            try:
                out.append(json.loads(value))
            except ValueError:
                continue
        return out

    def ids_in_range(self, from_ms, to_ms_exclusive):
        low = "-inf" if from_ms is None else str(from_ms)
        high = "+inf" if to_ms_exclusive is None else "(" + str(to_ms_exclusive)
        ids = self.cmd("ZRANGEBYSCORE", TIMELINE_KEY, low, high)
        return ids or []

    def events_by_ids(self, ids):
        out = []
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            values = self.cmd("HMGET", EVENTS_KEY, *chunk)
            for value in values or []:
                if not value:
                    continue
                try:
                    out.append(json.loads(value))
                except ValueError:
                    continue
        return out


def _pairs(raw):
    if isinstance(raw, dict):
        return list(raw.items())
    if isinstance(raw, list):
        return [(raw[i], raw[i + 1]) for i in range(0, len(raw) - 1, 2)]
    return []


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def load_credentials():
    if not os.path.exists(CRED_FILE):
        raise CliError(
            "credentials_missing",
            "未找到凭证文件 %s。请先向用户索取 Upstash URL 与 REST Token，再运行 init。" % CRED_FILE,
        )
    try:
        with open(CRED_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise CliError("credentials_invalid", "凭证文件无法解析：%s" % e)
    if not isinstance(data, dict):
        raise CliError("credentials_invalid", "凭证文件格式错误，应为 JSON 对象")
    url = str(data.get("url") or "").strip().rstrip("/")
    token = str(data.get("token") or "").strip()
    if not url or not token:
        raise CliError("credentials_invalid", "凭证文件缺少 url 或 token")
    return url, token


def make_client(ctx):
    url, token = load_credentials()
    return Client(url, token, no_proxy=ctx.get("no_proxy", False))


# --------------------------------------------------------------------- 日志

def log_op(command, records):
    """写入本地操作日志（每天一个 jsonl 文件），并清理 3 天前的旧文件。"""
    if not records:
        return
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        stamp = now_tz()
        path = os.path.join(LOG_DIR, stamp.strftime("%Y-%m-%d") + ".jsonl")
        with open(path, "a", encoding="utf-8") as f:
            for item in records:
                line = {"ts": iso(stamp), "command": command}
                line.update(item)
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        _rotate_logs(stamp)
    except OSError:
        pass


def _rotate_logs(stamp):
    """只保留最近 LOG_KEEP_DAYS 天的日志文件（含当天）。"""
    cutoff = stamp.date() - timedelta(days=LOG_KEEP_DAYS - 1)
    try:
        names = os.listdir(LOG_DIR)
    except OSError:
        return
    for name in names:
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.jsonl", name)
        if not m:
            continue
        try:
            day = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < cutoff:
            try:
                os.remove(os.path.join(LOG_DIR, name))
            except OSError:
                pass


# --------------------------------------------------------------------- 输入

def read_payload(args):
    """从 --json / --file / stdin 读取 JSON。"""
    text = None
    if getattr(args, "json", None):
        text = args.json
    elif getattr(args, "file", None):
        path = os.path.expanduser(args.file)
        if not os.path.exists(path):
            raise CliError("file_not_found", "文件不存在：%s" % path)
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    else:
        if sys.stdin is None or sys.stdin.isatty():
            raise CliError("no_input", "未提供输入：用 --file / --json 传，或从 stdin 管道输入")
        text = sys.stdin.read()

    text = (text or "").strip()
    if not text:
        raise CliError("no_input", "输入内容为空")
    try:
        return json.loads(text)
    except ValueError as e:
        raise CliError("invalid_payload", "输入不是合法 JSON：%s" % e)


def require_id(value):
    if not isinstance(value, str) or not value.strip():
        raise CliError("invalid_id", "缺少事件 id（--id evt_xxx）")
    return value.strip()


# --------------------------------------------------------------------- 命令

def cmd_init(args, ctx):
    url = (args.url or "").strip().rstrip("/")
    token = (args.token or "").strip()

    if not url:
        url = input("Upstash REST URL（https://xxx.upstash.io）：").strip().rstrip("/")
    if not token:
        token = input("Upstash REST Token：").strip()

    if not url or not token:
        raise CliError("invalid_credentials", "url 与 token 都不能为空")
    if not re.match(r"^https?://", url):
        raise CliError("invalid_credentials", "url 必须以 https:// 开头")

    existed = os.path.exists(CRED_FILE)
    if existed and not args.force:
        raise CliError(
            "credentials_exists",
            "凭证已存在：%s。如需覆盖请加 --force。" % CRED_FILE,
        )

    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CRED_FILE, "w", encoding="utf-8") as f:
        json.dump({"url": url, "token": token}, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(CRED_FILE, 0o600)
    except OSError:
        pass

    result = {
        "ok": True,
        "command": "init",
        "credentials_path": CRED_FILE,
        "created": True,
    }
    try:
        client = Client(url, token, no_proxy=ctx.get("no_proxy", False))
        pong = client.cmd("PING")
        result["connectivity"] = {"ok": True, "detail": str(pong)}
    except CliError as e:
        result["ok"] = False
        result["connectivity"] = {"ok": False, "error": e.code, "detail": e.message}
        result["message"] = "凭证已写入，但连通性验证失败，请核对 URL / Token。"
    return result


def cmd_doctor(args, ctx):
    checks = []

    if os.path.exists(CRED_FILE):
        try:
            url, token = load_credentials()
            checks.append({
                "name": "credentials",
                "status": "ok",
                "detail": "%s（url 已配置，token 长度 %d）" % (CRED_FILE, len(token)),
            })
        except CliError as e:
            url = token = None
            checks.append({"name": "credentials", "status": "error",
                           "error": e.code, "detail": e.message})
    else:
        url = token = None
        checks.append({"name": "credentials", "status": "error", "error": "credentials_missing",
                       "detail": "凭证文件不存在：%s" % CRED_FILE})

    client = None
    if url and token:
        client = Client(url, token, no_proxy=ctx.get("no_proxy", False))
        try:
            pong = client.cmd("PING")
            checks.append({"name": "connectivity", "status": "ok", "detail": str(pong)})
        except CliError as e:
            client = None
            checks.append({"name": "connectivity", "status": "error",
                           "error": e.code, "detail": e.message})
    else:
        checks.append({"name": "connectivity", "status": "skipped",
                       "detail": "凭证缺失，跳过连通性检查"})

    if client is not None:
        try:
            ev_type = client.cmd("TYPE", EVENTS_KEY)
            tl_type = client.cmd("TYPE", TIMELINE_KEY)
            ev_type = str(ev_type)
            tl_type = str(tl_type)
            problems = []
            if ev_type not in ("hash", "none"):
                problems.append("%s 类型为 %s，应为 hash" % (EVENTS_KEY, ev_type))
            if tl_type not in ("zset", "none"):
                problems.append("%s 类型为 %s，应为 zset" % (TIMELINE_KEY, tl_type))
            detail = {"events": "%s:%s" % (ev_type, client.cmd("HLEN", EVENTS_KEY) or 0),
                      "timeline": "%s:%s" % (tl_type, client.cmd("ZCARD", TIMELINE_KEY) or 0)}
            if problems:
                checks.append({"name": "storage", "status": "error",
                               "error": "key_type_mismatch", "detail": problems})
            elif ev_type == "none" or tl_type == "none":
                checks.append({"name": "storage", "status": "warn", "detail": detail,
                               "message": "键尚未创建，首次 add 时自动建立"})
            else:
                checks.append({"name": "storage", "status": "ok", "detail": detail})
        except CliError as e:
            checks.append({"name": "storage", "status": "error",
                           "error": e.code, "detail": e.message})

    local = now_tz()
    offset = local.utcoffset()
    clock = {
        "name": "clock",
        "local_offset": str(offset),
        "expected_offset": "+08:00",
    }
    problems = []
    if offset != timedelta(hours=8):
        problems.append("本机时区偏移为 %s，非东八区；写入的 ISO 时间会被强制换算为 +08:00" % offset)
    if client is not None:
        try:
            server_time = client.cmd("TIME")
            server_ms = int(server_time[0]) * 1000 + int(server_time[1]) // 1000
            skew = to_ms(now_tz()) - server_ms
            clock["skew_ms"] = skew
            if abs(skew) > 60000:
                problems.append("本机时间与 Upstash 服务器相差 %d 秒" % (skew // 1000))
        except (CliError, TypeError, IndexError, ValueError):
            clock["skew_ms"] = None
    if problems:
        clock["status"] = "warn"
        clock["detail"] = problems
    else:
        clock["status"] = "ok"
        clock["detail"] = "本机时区为东八区"
    checks.append(clock)

    ok = all(c["status"] in ("ok", "warn") for c in checks)
    return {"ok": ok, "command": "doctor", "checks": checks}


def cmd_add(args, ctx):
    payload = read_payload(args)
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        raise CliError("invalid_payload", "add 的输入必须是 JSON 数组")
    if not payload:
        raise CliError("empty_batch", "提交内容为空")
    if len(payload) > MAX_BATCH:
        raise CliError(
            "batch_too_large",
            "单次最多 %d 条，当前 %d 条，请分批提交" % (MAX_BATCH, len(payload)),
        )

    results = [None] * len(payload)
    prepared = []
    seen = {}

    for index, raw in enumerate(payload):
        try:
            norm = normalize_event(raw)
        except CliError as e:
            results[index] = {"index": index, "status": "error",
                              "error": e.code, "reason": e.message}
            continue
        key = (norm["title"], norm["time"])
        if key in seen:
            results[index] = {"index": index, "status": "skipped",
                              "title": norm["title"], "time": norm["time"],
                              "reason": "本批重复：与第 %d 条 title+time 相同" % seen[key]}
            continue
        seen[key] = index
        prepared.append((index, norm))

    if prepared:
        try:
            client = make_client(ctx)
        except CliError as e:
            for index, _ in prepared:
                results[index] = {"index": index, "status": "error",
                                  "error": "not_submitted", "reason": e.message}
            return {
                "ok": False,
                "command": "add",
                "error": e.code,
                "message": e.message,
                "total": len(results),
                "results": results,
            }
        events = [build_event(norm) for _, norm in prepared]
        client.write_events(events)
        for (index, _), event in zip(prepared, events):
            results[index] = {
                "index": index,
                "status": "ok",
                "id": event["id"],
                "title": event["title"],
                "time": event["time"],
            }

    ok_count = sum(1 for r in results if r["status"] == "ok")
    skipped_count = sum(1 for r in results if r["status"] == "skipped")
    error_count = sum(1 for r in results if r["status"] == "error")

    log_op("add", [
        {"id": r.get("id"), "title": r.get("title"),
         "result": r["status"], "error": r.get("error")}
        for r in results
    ])

    return {
        "ok": error_count == 0,
        "command": "add",
        "total": len(results),
        "ok_count": ok_count,
        "skipped_count": skipped_count,
        "error_count": error_count,
        "results": results,
    }


def cmd_update(args, ctx):
    event_id = require_id(args.id)
    patch = read_payload(args)
    if not isinstance(patch, dict):
        raise CliError("invalid_payload", "update 的输入必须是 JSON 对象")
    if not patch:
        raise CliError("empty_patch", "变更内容为空")
    for field in IMMUTABLE_FIELDS:
        if field in patch:
            raise CliError("immutable_field", "%s 由服务端维护，不可修改" % field)

    client = make_client(ctx)
    existing = client.get_event(event_id)
    if existing is None:
        raise CliError("not_found", "事件不存在：%s" % event_id)

    merged = dict(existing)
    for key, value in patch.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value

    norm = normalize_event(merged)
    stamp = iso_now()
    updated = {"schema_version": SCHEMA_VERSION, "id": event_id}
    updated.update(clean(norm))
    updated["created_at"] = existing.get("created_at") or stamp
    updated["updated_at"] = stamp

    if client.replace_event(updated) == 0:
        raise CliError("not_found", "事件不存在：%s" % event_id)

    log_op("update", [{"id": event_id, "fields": sorted(patch.keys()), "result": "ok"}])

    return {
        "ok": True,
        "command": "update",
        "id": event_id,
        "status": "ok",
        "changed": sorted(patch.keys()),
        "before": existing,
        "after": updated,
    }


def cmd_delete(args, ctx):
    event_id = require_id(args.id)
    client = make_client(ctx)
    removed = client.remove_event(event_id)
    log_op("delete", [{"id": event_id, "result": "deleted" if removed else "not_found"}])
    if removed == 0:
        raise CliError("not_found", "事件不存在：%s" % event_id)
    return {"ok": True, "command": "delete", "id": event_id, "status": "deleted"}


def cmd_query(args, ctx):
    """--from 含、--to 不含（开区间）；--to 只给日期时按整天处理。"""
    lower = None
    upper = None
    if args.from_:
        lower = to_ms(parse_time(args.from_, "--from"))
    if args.to:
        if is_date_only(args.to):
            upper = to_ms(parse_time(args.to, "--to") + timedelta(days=1))
        else:
            upper = to_ms(parse_time(args.to, "--to"))

    client = make_client(ctx)
    if lower is None and upper is None:
        events = client.all_events()
    else:
        ids = client.ids_in_range(lower, upper)
        events = client.events_by_ids(ids)

    events.sort(key=_sort_key)
    return {
        "ok": True,
        "command": "query",
        "from": iso(parse_time(args.from_)) if args.from_ else None,
        "to": args.to,
        "count": len(events),
        "events": events,
    }


def cmd_pull(args, ctx):
    client = make_client(ctx)
    events = client.all_events()
    events.sort(key=_sort_key)
    return {
        "ok": True,
        "command": "pull",
        "schema_version": SCHEMA_VERSION,
        "generated_at": iso_now(),
        "count": len(events),
        "events": events,
    }


def _sort_key(event):
    try:
        return (parse_time(event.get("time")).timestamp(), str(event.get("id") or ""))
    except CliError:
        return (float("inf"), str(event.get("id") or ""))


# --------------------------------------------------------------------- 入口

class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CliError("bad_arguments", message)


def build_parser():
    parser = Parser(prog="schedule.py", description="Upstash 日程同步 CLI")
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="写入凭证并验证连通性")
    p_init.add_argument("--url")
    p_init.add_argument("--token")
    p_init.add_argument("--force", action="store_true")

    sub.add_parser("doctor", help="自检：凭证、连通性、键结构、时钟")

    p_add = sub.add_parser("add", help="批量提交事件（≤20 条）")
    p_add.add_argument("--file")
    p_add.add_argument("--json")

    p_update = sub.add_parser("update", help="按 id 部分更新")
    p_update.add_argument("--id", required=True)
    p_update.add_argument("--file")
    p_update.add_argument("--json")

    p_delete = sub.add_parser("delete", help="按 id 删除")
    p_delete.add_argument("--id", required=True)

    p_query = sub.add_parser("query", help="查询事件")
    p_query.add_argument("--from", dest="from_")
    p_query.add_argument("--to")

    sub.add_parser("pull", help="全量导出（App 定时拉取）")

    return parser


def run(argv):
    no_proxy = "--no-proxy" in argv
    argv = [a for a in argv if a != "--no-proxy"]

    parser = build_parser()
    args = parser.parse_args(argv)
    ctx = {"no_proxy": no_proxy}

    handlers = {
        "init": cmd_init,
        "doctor": cmd_doctor,
        "add": cmd_add,
        "update": cmd_update,
        "delete": cmd_delete,
        "query": cmd_query,
        "pull": cmd_pull,
    }
    if not args.command:
        raise CliError("bad_arguments", "缺少命令，可用：init / doctor / add / update / delete / query / pull")

    return handlers[args.command](args, ctx)


def main(argv=None):
    try:
        result = run(argv if argv is not None else sys.argv[1:])
    except CliError as e:
        emit({"ok": False, "error": e.code, "message": e.message})
        return 1
    except KeyboardInterrupt:
        emit({"ok": False, "error": "interrupted", "message": "已中断"})
        return 1
    except Exception as e:  # noqa: BLE001 - 兜底，保证始终输出 JSON
        emit({"ok": False, "error": "internal_error", "message": "%s: %s" % (type(e).__name__, e)})
        return 1

    emit(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
