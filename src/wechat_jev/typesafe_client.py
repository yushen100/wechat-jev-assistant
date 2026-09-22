from __future__ import annotations

import os
import json
import subprocess
import sys
import time
import winreg
from dataclasses import dataclass
from typing import Any

import requests


API_URL = "https://api.typesafe.ai/v1/systemone"

QUESTIONS: dict[str, dict[str, Any]] = {
    "situation": {
        "type": "choice",
        "instructions": "结合 focus_message 与 messages，当前最新一条对方消息处于哪种沟通阶段？",
        "criteria": {
            "试探确认": "在确认用户是否记得、在乎、理解或重视某件事",
            "追问事实": "主要要求明确事实、答案、时间或安排",
            "表达不满": "正在表达失望、委屈、讽刺或责备",
            "施压升级": "明显警告、威胁、下最后通牒或把冲突推高",
            "缓和接受": "语气已经缓和，接受了解释、安排或补救",
            "玩笑调侃": "主要是轻松玩笑、调侃或低风险试探",
            "上下文不足": "证据不足，不能可靠判断当前阶段",
        },
    },
    "relationship_need": {
        "type": "choice",
        "instructions": "结合 focus_message 与此前上下文，对方此刻最需要用户提供什么？",
        "criteria": {
            "明确答案": "需要直接回答事实或问题",
            "被重视感": "需要确认自己被记得、被在乎或被认真对待",
            "承认疏忽": "需要用户先承认忘记、遗漏或考虑不周",
            "具体行动": "需要实际安排、承诺或可验证的下一步",
            "解释原因": "需要知道事情为何发生",
            "暂停空间": "情绪过高，当前更需要停止推进和留出空间",
            "无法判断": "上下文或识别可靠度不足",
        },
    },
    "cares_test": {
        "type": "noul",
        "instructions": "focus_message 是否主要在试探用户有没有记住、在乎或重视对方，而不只是询问字面事实？",
        "criteria": {
            "true": "关系确认和被重视感是核心",
            "false": "主要是普通事实问题、通知或其他目的",
        },
    },
    "concrete_reply_risk": {
        "type": "noul",
        "instructions": "如果用户现在立刻回答具体内容但不先处理情绪或关系信号，是否很可能让局势变差？",
        "criteria": {
            "true": "直接答题容易显得敷衍、狡辩或没有理解重点",
            "false": "直接给出具体答案是安全且合适的",
        },
    },
    "needs_repair_first": {
        "type": "noul",
        "instructions": "用户是否应该先承认疏忽或回应对方感受，再进行解释或给方案？",
        "criteria": {
            "true": "先修复情绪与关系比解释事实更重要",
            "false": "无需先道歉或安抚，可以直接处理事情",
        },
    },
    "conflict_emergency": {
        "type": "noul",
        "instructions": "当前对话是否已经进入高风险冲突状态，需要优先止损并避免继续辩解？",
        "criteria": {
            "true": "错误回应很可能立即升级冲突",
            "false": "仍是普通交流或可轻松缓和",
        },
    },
    "deescalated": {
        "type": "noul",
        "instructions": "focus_message 是否表明对方已经明显缓和或基本接受当前处理方向？",
        "criteria": {
            "true": "局势已明显降温，不再需要强力补救",
            "false": "仍未缓和，或证据不足",
        },
    },
    "stop_explaining": {
        "type": "noul",
        "instructions": "此刻继续补充解释或讨好是否可能画蛇添足，最稳妥的是停止扩展并落实行动？",
        "criteria": {
            "true": "继续说容易重新引发不满，应收住并行动",
            "false": "仍需要进一步解释、澄清或沟通",
        },
    },
    "literal_only": {
        "type": "noul",
        "instructions": "`messages` 中最后一条对方消息是否只表达字面含义，没有明显潜台词？",
        "criteria": {
            "true": "主要是直接询问、陈述或通知，不需要结合关系推断额外意图",
            "false": "结合最近对话可看出试探、情绪、期待或其他潜台词",
        },
    },
    "intent": {
        "type": "choice",
        "instructions": "结合 `messages`，最后一条对方消息的主要沟通意图是什么？",
        "criteria": {
            "询问信息": "主要希望获得事实、答案或说明",
            "寻求在乎": "希望确认自己被重视、被记住或被理解",
            "表达不满": "主要在表达失望、委屈或责备，但尚未明显升级冲突",
            "要求行动": "希望对方采取具体行动、承诺或安排",
            "玩笑试探": "以玩笑、调侃或轻度试探为主",
            "升级冲突": "明显激化争执、施压或准备摊牌",
            "无法判断": "上下文不足，或其他选项都不能可靠描述主要意图",
        },
    },
    "tension": {
        "type": "score",
        "instructions": "结合 `messages`，当前对话的紧张程度有多高？",
        "criteria": [
            "完全轻松，没有不满或压力",
            "日常交流，仅有极轻微情绪",
            "略有介意，但总体友好",
            "可感到不满，仍容易缓和",
            "明显不高兴，正在观察回应",
            "已经暗示或施压，需要认真处理",
            "接近争执，错误回应会明显恶化",
            "情绪紧绷，冲突可能马上升级",
            "一触即发，只差一个错误回应",
            "冲突已经爆发，需要优先止损",
        ],
    },
    "next_action": {
        "type": "choice",
        "instructions": "在不替用户生成具体回复的前提下，此刻最稳妥的下一步动作是什么？",
        "criteria": {
            "直接回答": "信息充分，应直接回应对方的问题或观点",
            "先查记录": "应先核对聊天记录或已知事实，避免凭空猜测",
            "承认疏忽": "重点是承认遗漏、忘记或没有顾及对方感受",
            "询问澄清": "上下文不足，应温和询问具体所指或期望",
            "暂停回应": "当前情绪过高，先冷静或等待更合适的时机",
            "无法判断": "现有信息不足以可靠推荐动作",
        },
    },
}

INTENT_CRITERIA = QUESTIONS["intent"]["criteria"]
MAX_PERSON_QUESTIONS = 6


def limit_messages_for_state(messages: list[dict[str, Any]], max_chars: int = 12000) -> list[dict[str, Any]]:
    """从最新消息倒序保留可安全提交的上下文，给 TypeSafe 32k state 上限留余量。"""
    selected: list[dict[str, Any]] = []
    used = 0
    for message in reversed(messages):
        size = len(str(message.get("speaker", ""))) + len(str(message.get("text", ""))) + 32
        if selected and used + size > max_chars:
            break
        selected.append(message)
        used += size
    return list(reversed(selected))


def select_focus_participants(
    messages: list[dict[str, Any]], max_people: int = MAX_PERSON_QUESTIONS
) -> tuple[list[str], int]:
    recent = messages[-40:]
    stats: dict[str, tuple[int, int]] = {}
    for index, message in enumerate(recent):
        speaker = str(message.get("speaker", "")).strip()
        if not speaker or speaker in {"我方", "对方", "未知群成员"}:
            continue
        count, _ = stats.get(speaker, (0, -1))
        stats[speaker] = (count + 1, index)
    ranked = sorted(
        stats,
        key=lambda speaker: (stats[speaker][1], stats[speaker][0]),
        reverse=True,
    )
    selected = ranked[:max(1, max_people)]
    return selected, max(0, len(ranked) - len(selected))


def build_reply_evaluation(
    messages: list[dict[str, Any]],
) -> tuple[int, dict[str, Any] | None]:
    self_indexes = [
        index for index, message in enumerate(messages)
        if message.get("is_self") or message.get("speaker") == "我方"
    ]
    for index in reversed(self_indexes):
        responses = [
            message for message in messages[index + 1:]
            if not message.get("is_self") and message.get("speaker") != "我方"
        ][:3]
        if responses:
            return len(self_indexes), {
                "reply_index": index,
                "reply": messages[index],
                "responses": responses,
                "context_before": messages[max(0, index - 5):index],
            }
    return len(self_indexes), None


def build_questions(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    questions = {key: dict(value) for key, value in QUESTIONS.items()}
    participants = [
        str(value) for value in state.get("participants", [])
        if value and value not in {"我方", "对方", "未知群成员"}
    ][:MAX_PERSON_QUESTIONS]
    if participants:
        for index, speaker in enumerate(participants):
            questions[f"person_intent_{index}"] = {
                "type": "choice",
                "instructions": (
                    f"结合完整的 `messages` 和 `participant_context.{speaker}`，判断 speaker 为 `{speaker}` "
                    "的人物在本轮发言中的主要沟通意图。只判断该人物，不要把其他人物的意图归给他/她；"
                    "sender_source、speaker_confidence、content_source 和 confidence 较低时应倾向无法判断。"
                ),
                "criteria": INTENT_CRITERIA,
            }
    if state.get("reply_evaluation"):
        questions["my_reply_relevance"] = {
            "type": "noul",
            "instructions": (
                "`reply_evaluation.responses` 是否确实在回应 `reply_evaluation.reply`？"
                "群聊中若只是其他人的无关发言，应判断为 false。"
            ),
            "criteria": {
                "true": "后续内容与我的回答存在明确语义承接、反应或反馈",
                "false": "后续内容无关、无法归因，或证据不足",
            },
        }
        questions["my_reply_outcome"] = {
            "type": "choice",
            "instructions": (
                "结合 `reply_evaluation`，我的这次回答对后续交流产生了什么主要效果？"
                "若后续消息与回答无关，选择无法判断。"
            ),
            "criteria": {
                "明显改善": "对方更理解、接受、缓和或积极配合",
                "有效推进": "回答解决了问题或让交流进入下一步",
                "影响有限": "对方有回应，但没有明显改善或恶化",
                "引起误解": "回答让对方困惑、需要重复解释或偏离重点",
                "造成恶化": "回答引发更强不满、冲突、拒绝或压力",
                "无法判断": "没有相关后续回复，或群聊消息无法可靠归因",
            },
        }
        questions["my_reply_effectiveness"] = {
            "type": "score",
            "instructions": (
                "结合 `reply_evaluation.reply` 与对方相关后续反应，评价我的回答有效性。"
                "只评价实际效果，不评价文采；后续无法归因时应倾向最低档。"
            ),
            "criteria": [
                "没有可归因的后续反馈，无法有效评分",
                "几乎无效，明显没有回应对方重点",
                "效果很差，引起明显困惑或反感",
                "效果偏差，需要较多补充或纠正",
                "略有作用，但核心问题仍未解决",
                "基本合格，部分回应了对方需求",
                "较为有效，交流得到实际推进",
                "效果良好，对方明显理解或接受",
                "非常有效，准确处理重点并改善互动",
                "效果极佳，对方明确积极回应且问题得到解决",
            ],
        }
    return questions


class TypeSafeError(RuntimeError):
    pass


@dataclass(slots=True)
class TypeSafeResult:
    payload: dict[str, Any]
    elapsed_seconds: float
    fallback_used: bool = False


def compact_state_for_retry(state: dict[str, Any]) -> dict[str, Any]:
    """超时后缩减上下文和人物问题数量，避免原样重复一个过重请求。"""
    compact = dict(state)
    compact["messages"] = list(state.get("messages", []))[-40:]
    participants = [str(value) for value in state.get("participants", []) if value][:3]
    compact["participants"] = participants
    original_context = state.get("participant_context", {})
    compact["participant_context"] = {
        speaker: list(original_context.get(speaker, []))[-4:]
        for speaker in participants
    }
    return compact
    fallback_used: bool = False


def compact_state_for_retry(state: dict[str, Any]) -> dict[str, Any]:
    """超时后缩减上下文和人物问题数量，避免原样重复一个过重请求。"""
    compact = dict(state)
    messages = list(state.get("messages", []))[-40:]
    participants = [str(value) for value in state.get("participants", []) if value][:3]
    original_context = state.get("participant_context", {})
    compact["messages"] = messages
    compact["participants"] = participants
    compact["participant_context"] = {
        speaker: list(original_context.get(speaker, []))[-4:]
        for speaker in participants
    }
    return compact


class TypeSafeClient:
    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout

    def evaluate(self, state: dict[str, Any]) -> TypeSafeResult:
        api_key = os.getenv("TYPESAFE_API_KEY") or self._read_user_environment_key()
        if not api_key:
            raise TypeSafeError("缺少环境变量 TYPESAFE_API_KEY")

        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        started = time.perf_counter()
        last_error: Exception | None = None
        request_state = state
        fallback_used = False
        for attempt in range(2):
            try:
                body = {
                    "state": request_state,
                    "model": "jev-latest",
                    "questions": build_questions(request_state),
                }
                response = requests.post(
                    API_URL,
                    headers=headers,
                    json=body,
                    timeout=(5.0, self.timeout),
                )
                if response.status_code in {429, 500, 502, 503, 504} and attempt == 0:
                    time.sleep(0.8)
                    continue
                if response.status_code == 401:
                    raise TypeSafeError("TypeSafe API key 无效或已失效")
                if response.status_code == 429:
                    raise TypeSafeError("TypeSafe 请求过于频繁，请稍后重试")
                if response.status_code == 422:
                    raise TypeSafeError("TypeSafe 请求格式校验失败")
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload.get("answers"), dict):
                    raise TypeSafeError("TypeSafe 返回缺少 answers")
                return TypeSafeResult(
                    payload=payload,
                    elapsed_seconds=time.perf_counter() - started,
                    fallback_used=fallback_used,
                )
            except TypeSafeError:
                raise
            except requests.ReadTimeout as exc:
                last_error = exc
                if attempt == 0:
                    request_state = compact_state_for_retry(state)
                    fallback_used = True
                    time.sleep(0.4)
                    continue
                raise TypeSafeError(
                    "TypeSafe 响应超时；已用精简上下文重试。请检查网络或稍后再试"
                ) from exc
            except (requests.ConnectTimeout, requests.ConnectionError) as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.6)
                    continue
                raise TypeSafeError("无法连接 TypeSafe；请检查网络、代理或防火墙") from exc
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.8)
                    continue
        raise TypeSafeError(f"TypeSafe 请求失败：{type(last_error).__name__}")

    def evaluate_isolated(self, state: dict[str, Any]) -> TypeSafeResult:
        request_bytes = json.dumps(state, ensure_ascii=True).encode("ascii")
        try:
            completed = subprocess.run(
                [sys.executable, __file__, "--worker"],
                input=request_bytes,
                capture_output=True,
                timeout=self.timeout * 2 + 5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TypeSafeError("TypeSafe 请求超时，已终止本次分析") from exc
        stdout = completed.stdout.decode("utf-8", errors="replace").strip()
        try:
            envelope = json.loads(stdout or "{}")
        except ValueError as exc:
            raise TypeSafeError("TypeSafe 隔离进程返回异常") from exc
        if completed.returncode != 0 or not envelope.get("ok"):
            raise TypeSafeError(str(envelope.get("error") or "TypeSafe 隔离进程失败"))
        return TypeSafeResult(
            payload=dict(envelope["payload"]),
            elapsed_seconds=float(envelope["elapsed_seconds"]),
            fallback_used=bool(envelope.get("fallback_used", False)),
        )

    @staticmethod
    def _read_user_environment_key() -> str | None:
        """读取 Windows 用户环境变量，解决常驻父进程环境未刷新的情况。"""
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value, _ = winreg.QueryValueEx(key, "TYPESAFE_API_KEY")
                return str(value).strip() or None
        except (FileNotFoundError, OSError):
            return None


def _worker_main() -> int:
    try:
        state = json.loads(sys.stdin.buffer.read().decode("ascii"))
        result = TypeSafeClient().evaluate(state)
        envelope = {
            "ok": True,
            "payload": result.payload,
            "elapsed_seconds": result.elapsed_seconds,
            "fallback_used": result.fallback_used,
        }
    except Exception as exc:
        envelope = {"ok": False, "error": str(exc), "error_type": type(exc).__name__}
    sys.stdout.write(json.dumps(envelope, ensure_ascii=True))
    return 0 if envelope.get("ok") else 1


if __name__ == "__main__" and "--worker" in sys.argv:
    raise SystemExit(_worker_main())
