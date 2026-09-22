from __future__ import annotations

import argparse
from difflib import SequenceMatcher
import importlib
import json
import shutil
import sys
import tempfile
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "source"
PACKAGE = SOURCE / "wechatauto"
RUNTIME = ROOT / "runtime"
PINNED_COMMIT = "424ffdeeaa9e695191007614703ce78b96e7e6a9e"


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift < 70:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError("invalid protobuf varint")


def _protobuf_length_fields(data: bytes) -> list[tuple[int, bytes]]:
    fields = []
    offset = 0
    while offset < len(data):
        key, offset = _read_varint(data, offset)
        field_number, wire_type = key >> 3, key & 7
        if wire_type == 0:
            _, offset = _read_varint(data, offset)
        elif wire_type == 1:
            offset += 8
        elif wire_type == 2:
            size, offset = _read_varint(data, offset)
            end = offset + size
            if end > len(data):
                break
            fields.append((field_number, data[offset:end]))
            offset = end
        elif wire_type == 5:
            offset += 4
        else:
            break
    return fields


def _group_display_names(blob: bytes) -> dict[str, str]:
    result = {}
    for field_number, record in _protobuf_length_fields(blob):
        if field_number != 1:
            continue
        values = {}
        for inner_number, value in _protobuf_length_fields(record):
            if inner_number in {1, 2}:
                values[inner_number] = value.decode("utf-8", errors="ignore").strip()
        username = values.get(1, "")
        display_name = values.get(2, "")
        if username and display_name:
            result[username] = display_name
    return result


def _load_group_display_names(db, chatroom_username: str) -> dict[str, str]:
    for rel, path, _ in db._db_files:
        if Path(path).name != "contact.db":
            continue
        connection = db._open(rel)
        try:
            row = connection.execute(
                "SELECT ext_buffer FROM chat_room WHERE username=? LIMIT 1",
                (chatroom_username,),
            ).fetchone()
            if row and row[0]:
                return _group_display_names(bytes(row[0]))
        finally:
            connection.close()
    return {}


def _member_display_name(member: dict[str, object], group_name: str = "") -> str:
    """备注名优先；有差异时保留群昵称，避免人物对应关系丢失。"""
    remark = str(member.get("remark") or "").strip()
    group_name = str(group_name or "").strip()
    nickname = str(member.get("nick_name") or "").strip()
    if remark:
        if group_name and group_name != remark:
            return f"{remark}（群昵称：{group_name}）"
        return remark
    return group_name or nickname


def _load_db_module():
    """只加载 db.py，禁止执行上游会导入发送/UIA/媒体模块的 __init__.py。"""
    source_text = (PACKAGE / "db.py").read_text(encoding="utf-8")
    forbidden = ("WriteProcessMemory", "CreateRemoteThread", "VirtualAllocEx")
    found = [name for name in forbidden if name in source_text]
    if found:
        raise RuntimeError("只读安全检查失败：" + "、".join(found))

    package = types.ModuleType("wechatauto")
    package.__path__ = [str(PACKAGE)]
    package.__package__ = "wechatauto"
    sys.modules["wechatauto"] = package
    sys.path.insert(0, str(SOURCE))
    module = importlib.import_module("wechatauto.db")
    # 上游默认把数据库密钥持久化到明文 keys.json；只读桥接器禁止落盘。
    module.WeChatDB._save_keys = lambda self: None
    return module


def probe() -> dict[str, object]:
    module = _load_db_module()
    RUNTIME.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="probe-", dir=RUNTIME))
    try:
        db = module.WeChatDB(
            workdir=str(temporary / "decrypted"),
            keys_file=str(temporary / "keys.json"),
        )
        sessions = db.get_sessions(limit=200)
        sampled_messages = 0
        if sessions:
            username = str(sessions[0].get("username", ""))
            if username:
                sampled_messages = len(db.get_messages(username, limit=3))
        return {
            "ok": True,
            "pinned_commit": PINNED_COMMIT,
            "account_detected": bool(getattr(db, "account", "")),
            "database_files": len(getattr(db, "_db_files", [])),
            "sessions": len(sessions),
            "sampled_messages": sampled_messages,
            "plaintext_retained": False,
            "keys_persisted": False,
        }
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _normal(value: object) -> str:
    import re
    return re.sub(r"[^0-9A-Za-z一-鿿]+", "", str(value or "")).lower()


def _title_key(value: object) -> str:
    """去掉微信群标题末尾的人数；人数最容易被标题 OCR 误识别。"""
    import re
    clean = "".join(
        "\ufffd" if 0xD800 <= ord(char) <= 0xDFFF else char
        for char in str(value or "")
    ).strip()
    clean = re.sub(r"\s*[（(/]\d+[）)]?\s*$", "", clean).strip()
    return _normal(clean)


def _title_member_count(value: object) -> int | None:
    """提取微信群标题末尾人数，OCR 使用斜杠时也兼容。"""
    import re
    match = re.search(r"[（(/](\d{1,4})[）)]?\s*$", str(value or "").strip())
    return int(match.group(1)) if match else None


def _group_sender_from_content(
    content: str, members: dict[str, str]
) -> tuple[str | None, str]:
    """微信 4.x 群消息正文常带 ``wxid_xxx:\n正文``，这是最可靠的发送者来源。"""
    import re
    match = re.match(r"^\s*([A-Za-z0-9_.@-]+):\s*(?:\r?\n)?", content)
    if not match:
        return None, content
    username = match.group(1)
    if username not in members:
        return None, content
    return username, content[match.end():].lstrip()


def _similar(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    if min(len(left), len(right)) < 4:
        return False
    return SequenceMatcher(None, left, right).ratio() >= 0.78


def query(payload: dict[str, object]) -> dict[str, object]:
    module = _load_db_module()
    raw_title = "".join(
        "\ufffd" if 0xD800 <= ord(char) <= 0xDFFF else char
        for char in str(payload.get("title", ""))
    ).strip()
    title = _title_key(raw_title)
    expected_member_count = _title_member_count(raw_title)
    limit = max(1, min(500, int(payload.get("limit", 100))))
    if not title:
        raise RuntimeError("会话标题为空")
    RUNTIME.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="query-", dir=RUNTIME))
    try:
        db = module.WeChatDB(
            workdir=str(temporary / "decrypted"),
            keys_file=str(temporary / "keys.json"),
        )
        sessions = db.get_sessions(limit=5000)
        session_users = {str(session.get("username", "")) for session in sessions}
        # 不能先用 OCR 标题做 SQL LIKE：只要 OCR 错一个字就会得到空集合。
        # 先限定到最近会话，再在本地对真实显示名做模糊比较。
        nickname_index = db._nickname_index()
        candidates = []
        for username in session_users:
            if not username:
                continue
            display = str(nickname_index.get(username) or username)
            normalized = _title_key(display)
            score = 1.0 if normalized == title else SequenceMatcher(None, normalized, title).ratio()
            candidates.append((score, username, display))
        candidates.sort(reverse=True)
        if not candidates or candidates[0][0] < 0.78:
            raise RuntimeError(f"数据库中找不到会话标题：{raw_title}")
        plausible = [candidate for candidate in candidates if candidate[0] >= 0.78]
        exact_matches = [
            candidate for candidate in plausible
            if _title_key(candidate[2]) == title
        ]
        if len(exact_matches) > 1 and expected_member_count is None:
            raise RuntimeError(f"存在多个同名会话，缺少成员数，已拒绝猜测：{raw_title}")
        if expected_member_count is not None:
            count_matches = []
            for candidate in plausible:
                _, candidate_username, _ = candidate
                if not candidate_username.endswith("@chatroom"):
                    continue
                member_count = len(db.get_group_members(candidate_username))
                if abs(member_count - expected_member_count) <= 1:
                    count_matches.append(candidate)
            if len(count_matches) == 1:
                candidates = count_matches
            elif len(count_matches) > 1:
                raise RuntimeError(f"存在多个同名且人数相同的群聊：{raw_title}")
            elif len(plausible) > 1:
                raise RuntimeError(f"同名群聊无法通过成员数确认：{raw_title}")
        if (
            len(candidates) > 1
            and candidates[0][0] < 0.999
            and candidates[0][0] - candidates[1][0] < 0.08
        ):
            raise RuntimeError(f"会话标题匹配不唯一：{raw_title}")
        _, username, matched_title = candidates[0]
        usable_types = {"文本", "动画表情"}
        usable_rows = []
        offset = 0
        page_size = max(500, limit * 5)
        while len(usable_rows) < limit and offset < 5000:
            page = db.get_messages(username, limit=page_size, offset=offset)
            if not page:
                break
            usable_rows.extend(
                row for row in page
                if str(row.get("type")) in usable_types
            )
            offset += len(page)
            if len(page) < page_size:
                break
        usable_rows = usable_rows[:limit]
        rows = list(reversed(usable_rows))
        if not rows:
            raise RuntimeError("该会话没有可读取的文字消息")
        is_group = username.endswith("@chatroom")
        group_members = {}
        if is_group:
            group_display_names = _load_group_display_names(db, username)
            group_members = {
                str(member.get("username") or ""): _member_display_name(
                    member,
                    group_display_names.get(str(member.get("username") or ""), ""),
                )
                for member in db.get_group_members(username)
            }
        messages = []
        invalid_group_senders = set()
        for order, row in enumerate(rows):
            is_self = int(row.get("sender_id") or 0) == 2
            content = str(row.get("content") or "")
            if is_self:
                speaker = "我方"
            elif is_group:
                content_sender, content = _group_sender_from_content(content, group_members)
                sender = content_sender or str(row.get("sender_username") or "")
                if not content_sender and sender and sender not in group_members:
                    invalid_group_senders.add(sender)
                speaker = group_members.get(sender, "")
                if not speaker and sender:
                    fallback = str(db.get_nickname(sender) or "").strip()
                    if fallback and fallback != sender and not fallback.startswith("wxid_"):
                        speaker = fallback
                if not speaker or speaker.startswith("wxid_"):
                    speaker = "未知群成员"
            else:
                speaker = matched_title
            msg_type = str(row.get("type") or "文本")
            message_type = "sticker" if msg_type == "动画表情" else "text"
            if message_type == "sticker":
                content = "[动画表情]"
            message_id = f"{int(row.get('sort_seq') or 0)}:{int(row.get('local_id') or 0)}"
            messages.append({
                "speaker": speaker,
                "text": content,
                "order": order,
                "confidence": 1.0,
                "is_self": is_self,
                "speaker_confidence": 1.0 if speaker != "未知群成员" else 0.35,
                "message_type": message_type,
                "sender_source": "wechat_database",
                "content_source": f"wechat_database:{msg_type}",
                "visual_fingerprint": "",
                "message_id": message_id,
                "created_at": int(row.get("create_time") or 0),
                "sender_id": "self" if is_self else str(sender or "") if is_group else username,
            })
        if is_group and invalid_group_senders:
            raise RuntimeError("数据库消息发送者与目标群成员表不一致，已拒绝分析以防串会话")
        return {
            "ok": True,
            "matched_title": matched_title,
            "conversation_id": username,
            "conversation_type": "group" if is_group else "private",
            "messages": messages,
            "keys_persisted": False,
            "plaintext_retained": False,
        }
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="微信数据库只读隔离桥接器")
    parser.add_argument("--probe", action="store_true", help="仅输出无正文诊断统计")
    parser.add_argument("--query-stdin", action="store_true", help="从标准输入接收会话查询")
    args = parser.parse_args()
    try:
        if args.probe:
            result = probe()
        elif args.query_stdin:
            result = query(json.loads(sys.stdin.read()))
        else:
            parser.error("必须指定 --probe 或 --query-stdin")
    except Exception as exc:
        result = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
    # 保持跨进程输出为纯 ASCII；中文和异常 Unicode 均用 JSON 转义表示。
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
