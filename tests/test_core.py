from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wechat_jev.uia_title import UiaText, choose_header_title  # noqa: E402
from integrations.wechatauto_readonly.readonly_bridge import (  # noqa: E402
    _group_display_names,
    _group_sender_from_content,
    _member_display_name,
    _title_key,
    _title_member_count,
)
from wechat_jev.crypto_store import EncryptedHistoryStore  # noqa: E402
from wechat_jev.capture import (  # noqa: E402
    RapidOcrEngine,
    WindowContext,
    _hash_distance,
    _perceptual_hash,
    _scroll_notches_for_region,
    display_profile,
    merge_older_page,
)
from wechat_jev.config import MESSAGE_LIMIT_OPTIONS, AppConfig  # noqa: E402
from wechat_jev.conversation_memory import (  # noqa: E402
    MEMORY_PARSER_VERSION,
    canonicalize_speakers,
    find_matching_memory,
    message_anchor,
    merge_with_memory,
)
from wechat_jev.models import CaptureRegion, ChatMessage  # noqa: E402
from wechat_jev.privacy import redact_text  # noqa: E402
from wechat_jev.region_selector import region_from_points  # noqa: E402
from wechat_jev.typesafe_client import TypeSafeClient, TypeSafeError, build_questions, build_reply_evaluation, limit_messages_for_state, select_focus_participants  # noqa: E402


class PrivacyTests(unittest.TestCase):
    def test_redacts_sensitive_values(self) -> None:
        text = "电话13800138000，邮箱a@test.com，身份证11010519491231002X，卡6222 0200 1234 5678 9，https://a.cn/p?a=1"
        result = redact_text(text)
        self.assertNotIn("13800138000", result)
        self.assertNotIn("a@test.com", result)
        self.assertNotIn("11010519491231002X", result)
        self.assertNotIn("a=1", result)
        self.assertIn("[手机号]", result)


class RegionTests(unittest.TestCase):
    def test_default_region_is_valid(self) -> None:
        CaptureRegion().validate()

    def test_too_small_region_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CaptureRegion(left=0.1, top=0.1, right=0.2, bottom=0.2).validate()

    def test_drag_points_convert_to_relative_region(self) -> None:
        region = region_from_points((800, 700), (200, 100), 1000, 800)
        self.assertEqual((region.left, region.top, region.right, region.bottom), (0.2, 0.125, 0.8, 0.875))

    @patch("wechat_jev.capture.ctypes.windll.user32.GetDpiForWindow", return_value=144)
    @patch("wechat_jev.capture.win32api.GetMonitorInfo", return_value={"Monitor": (0, 0, 2048, 1152)})
    @patch("wechat_jev.capture.win32api.MonitorFromWindow", return_value=1)
    def test_display_profile_uses_win32api(self, _monitor: Mock, _info: Mock, _dpi: Mock) -> None:
        context = WindowContext(hwnd=123, rect=(0, 0, 1000, 800), title="测试")
        self.assertEqual(display_profile(context), "2048x1152@144")


class GroupOcrTests(unittest.TestCase):
    def test_member_name_prefers_remark_then_group_name_then_nickname(self) -> None:
        member = {"remark": "我的备注", "nick_name": "微信昵称"}
        self.assertEqual(
            _member_display_name(member, "群内昵称"),
            "我的备注（群昵称：群内昵称）",
        )
        member["remark"] = ""
        self.assertEqual(_member_display_name(member, "群内昵称"), "群内昵称")
        self.assertEqual(_member_display_name(member, ""), "微信昵称")

    def test_group_display_name_is_decoded_from_ext_buffer(self) -> None:
        username = "wxid_example_member".encode("utf-8")
        display_name = "示例成员甲".encode("utf-8")
        record = bytes([0x0A, len(username)]) + username + bytes([0x12, len(display_name)]) + display_name
        blob = bytes([0x0A, len(record)]) + record
        self.assertEqual(_group_display_names(blob), {"wxid_example_member": "示例成员甲"})

    def test_title_key_removes_slash_style_member_count(self) -> None:
        self.assertEqual(_title_key("示例讨论群/03）"), "示例讨论群")
        self.assertEqual(_title_member_count("示例讨论群(93)"), 93)
        self.assertEqual(_title_member_count("示例讨论群/03）"), 3)

    def test_group_sender_prefix_is_used_and_removed_from_text(self) -> None:
        sender, text = _group_sender_from_content(
            "wxid_example_sender:\n明天晚上的活动提醒",
            {"wxid_example_sender": "示例成员乙"},
        )
        self.assertEqual(sender, "wxid_example_sender")
        self.assertEqual(text, "明天晚上的活动提醒")

    def test_title_ocr_replaces_invalid_unicode_surrogate(self) -> None:
        def box(x1: int, y1: int, x2: int, y2: int) -> list[list[int]]:
            return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]

        engine = RapidOcrEngine.__new__(RapidOcrEngine)
        engine._engine = Mock(return_value=(
            [[box(100, 10, 500, 40), "测试群\udcaa(87)", 0.99]],
            None,
        ))
        title = engine.recognize_title(Image.new("RGB", (600, 60), "white"))
        title.encode("utf-8")
        self.assertNotIn("\udcaa", title)

    def test_uia_header_title_ignores_sidebar_and_window_buttons(self) -> None:
        items = [
            UiaText("联系人", 20, 45, 80, 70),
            UiaText("示例讨论群 (93)", 220, 48, 500, 76),
            UiaText("更多", 940, 45, 980, 75),
            UiaText("示例成员乙", 100, 180, 180, 205),
        ]
        title = choose_header_title(items, (0, 0, 1000, 900))
        self.assertEqual(title, "示例讨论群 (93)")

    def test_group_sender_labels_are_attached_to_messages(self) -> None:
        def box(x1: int, y1: int, x2: int, y2: int) -> list[list[int]]:
            return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]

        ocr_result = [
            [box(20, 10, 60, 20), "张三", 0.99],
            [box(30, 29, 180, 51), "大家今晚吃什么？", 0.98],
            [box(22, 72, 62, 82), "李四", 0.97],
            [box(32, 91, 145, 113), "我想吃火锅", 0.96],
            [box(650, 132, 760, 154), "我都可以", 0.99],
        ]
        engine = RapidOcrEngine.__new__(RapidOcrEngine)
        engine._engine = Mock(return_value=(ocr_result, None))
        messages, _, warnings = engine.recognize(Image.new("RGB", (800, 200), "white"), 8)
        self.assertEqual([message.speaker for message in messages], ["张三", "李四", "我方"])
        self.assertEqual(messages[0].text, "大家今晚吃什么？")
        self.assertTrue(any("2 位发言者" in warning for warning in warnings))

    def test_visual_message_uses_left_or_right_position(self) -> None:
        image = Image.new("RGB", (800, 300), (240, 240, 240))
        for y in range(70, 170):
            for x in range(500, 700):
                image.putpixel((x, y), ((x * 3) % 255, (y * 5) % 255, (x + y) % 255))
        engine = RapidOcrEngine.__new__(RapidOcrEngine)
        engine._engine = Mock(return_value=([], None))
        messages, confidence, _ = engine.recognize(image, 8)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].speaker, "我方")
        self.assertEqual(messages[0].text, "[图片或表情，内容不确定]")
        self.assertEqual(messages[0].message_type, "visual")
        self.assertEqual(messages[0].content_source, "placeholder")
        self.assertTrue(messages[0].visual_fingerprint)
        self.assertGreater(confidence, 0.5)

    def test_group_nickname_is_attached_to_visual_message(self) -> None:
        def box(x1: int, y1: int, x2: int, y2: int) -> list[list[int]]:
            return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]

        image = Image.new("RGB", (800, 300), (240, 240, 240))
        for y in range(80, 190):
            for x in range(80, 280):
                image.putpixel((x, y), ((x * 2) % 255, (y * 3) % 255, (x + y * 2) % 255))
        engine = RapidOcrEngine.__new__(RapidOcrEngine)
        engine._engine = Mock(return_value=([[box(80, 50, 130, 62), "张三", 0.98]], None))
        messages, _, _ = engine.recognize(image, 8)
        self.assertEqual(messages[-1].speaker, "张三")
        self.assertEqual(messages[-1].text, "[图片或表情，内容不确定]")
        self.assertEqual(messages[-1].sender_source, "nickname_binding")

    def test_text_inside_picture_is_not_treated_as_nickname(self) -> None:
        def box(x1: int, y1: int, x2: int, y2: int) -> list[list[int]]:
            return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]

        image = Image.new("RGB", (800, 320), (240, 240, 240))
        for y in range(90, 220):
            for x in range(80, 320):
                image.putpixel((x, y), ((x * 2) % 255, (y * 3) % 255, (x + y) % 255))
        ocr_result = [
            [box(80, 55, 130, 67), "张三", 0.98],
            [box(130, 130, 210, 150), "小可爱", 0.96],
        ]
        engine = RapidOcrEngine.__new__(RapidOcrEngine)
        engine._engine = Mock(return_value=(ocr_result, None))
        messages, _, _ = engine.recognize(image, 8)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].speaker, "张三")
        self.assertIn("小可爱", messages[0].text)
        self.assertEqual(messages[0].content_source, "image_ocr")

    def test_avatar_column_is_excluded_and_large_media_still_binds_to_name(self) -> None:
        def box(x1: int, y1: int, x2: int, y2: int) -> list[list[int]]:
            return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]

        image = Image.new("RGB", (1007, 1163), (31, 31, 31))
        # 左侧头像与下方媒体内容同时存在，不能合并为同一个视觉块。
        for y in range(810, 855):
            for x in range(30, 75):
                image.putpixel((x, y), ((x * 7) % 255, (y * 3) % 255, 80))
        for y in range(923, 1060):
            for x in range(94, 260):
                image.putpixel((x, y), ((x * 3) % 255, (y * 5) % 255, (x + y) % 255))
        engine = RapidOcrEngine.__new__(RapidOcrEngine)
        engine._engine = Mock(return_value=([[box(90, 808, 162, 832), "示例成员丙", 0.99]], None))
        messages, _, _ = engine.recognize(image, 8)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].speaker, "示例成员丙")
        self.assertEqual(messages[0].sender_source, "nickname_binding")

    def test_plain_text_bubble_is_not_classified_as_picture(self) -> None:
        def box(x1: int, y1: int, x2: int, y2: int) -> list[list[int]]:
            return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]

        image = Image.new("RGB", (800, 220), (240, 240, 240))
        for y in range(70, 115):
            for x in range(520, 720):
                image.putpixel((x, y), (149, 236, 105))
        engine = RapidOcrEngine.__new__(RapidOcrEngine)
        engine._engine = Mock(return_value=([[box(550, 80, 690, 105), "这是一条文字消息", 0.98]], None))
        messages, _, _ = engine.recognize(image, 8)
        self.assertEqual(messages[0].message_type, "text")
        self.assertEqual(messages[0].text, "这是一条文字消息")

    def test_dark_theme_short_bubbles_and_group_names_are_separated(self) -> None:
        def box(x1: int, y1: int, x2: int, y2: int) -> list[list[int]]:
            return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]

        image = Image.new("RGB", (982, 420), (31, 31, 31))
        for y in range(152, 197):
            for x in range(59, 171):
                image.putpixel((x, y), (45, 45, 45))
        for y in range(222, 268):
            for x in range(807, 924):
                image.putpixel((x, y), (44, 210, 140))
        ocr_result = [
            [box(80, 126, 150, 148), "示例成员丁", 0.99],
            [box(96, 162, 147, 189), "你是？", 0.99],
            [box(821, 229, 888, 261), "羡慕", 0.98],
            [box(856, 390, 948, 410), "1条新消息", 0.99],
        ]
        engine = RapidOcrEngine.__new__(RapidOcrEngine)
        engine._engine = Mock(return_value=(ocr_result, None))
        messages, _, _ = engine.recognize(image, 8)
        self.assertEqual(
            [(message.speaker, message.text, message.message_type) for message in messages],
            [("示例成员丁", "你是？", "text"), ("我方", "羡慕", "text")],
        )


class MultiPageCaptureTests(unittest.TestCase):
    def test_default_reads_one_hundred_messages(self) -> None:
        self.assertEqual(AppConfig().max_messages, 100)

    def test_message_limit_has_four_visible_levels(self) -> None:
        self.assertEqual(MESSAGE_LIMIT_OPTIONS, (100, 150, 200, 250))

    def test_scroll_distance_uses_calibrated_region_height(self) -> None:
        context = WindowContext(hwnd=1, rect=(0, 0, 1000, 1000), title="测试")
        short = CaptureRegion(left=0.2, top=0.2, right=0.9, bottom=0.6)
        tall = CaptureRegion(left=0.2, top=0.1, right=0.9, bottom=0.9)
        self.assertGreater(
            _scroll_notches_for_region(context, tall),
            _scroll_notches_for_region(context, short),
        )
        self.assertEqual(_scroll_notches_for_region(context, tall), 13)

    def test_overlapping_pages_are_merged_in_order(self) -> None:
        def message(text: str) -> ChatMessage:
            return ChatMessage("对方", text, 0, 0.9)

        older = [message("第一条"), message("第二条"), message("第三条"), message("第四条")]
        newer = [message("第三条"), message("第四条"), message("第五条")]
        merged = merge_older_page(older, newer)
        self.assertEqual(
            [item.text for item in merged],
            ["第一条", "第二条", "第三条", "第四条", "第五条"],
        )
        self.assertEqual([item.order for item in merged], list(range(5)))

    def test_visual_fingerprint_deduplicates_overlap_even_if_sender_differs(self) -> None:
        older = [
            ChatMessage("未知群成员", "[图片]", 0, 0.5, visual_fingerprint="0000000000000000", message_type="visual"),
            ChatMessage("未知群成员", "[图片]", 1, 0.5, visual_fingerprint="1111111111111111", message_type="visual"),
        ]
        newer = [
            ChatMessage("张三", "[图片]", 0, 0.8, visual_fingerprint="0000000000000001", message_type="visual"),
            ChatMessage("张三", "[图片]", 1, 0.8, visual_fingerprint="1111111111111110", message_type="visual"),
        ]
        merged = merge_older_page(older, newer)
        self.assertEqual(len(merged), 2)

    def test_fuzzy_ocr_overlap_removes_repeated_screen(self) -> None:
        def message(text: str) -> ChatMessage:
            return ChatMessage("对方", text, 0, 0.9)

        older = [message("前文消息"), message("今晚一起吃饭"), message("明天再见")]
        newer = [message("今晚一起吃坂"), message("明天再见"), message("后续消息")]
        merged = merge_older_page(older, newer)
        self.assertEqual(
            [item.text for item in merged],
            ["前文消息", "今晚一起吃饭", "明天再见", "后续消息"],
        )

    def test_single_repeated_message_is_preserved_without_two_anchors(self) -> None:
        def message(text: str) -> ChatMessage:
            return ChatMessage("对方", text, 0, 0.9)

        merged = merge_older_page(
            [message("上一句"), message("好的")],
            [message("好的"), message("下一句")],
        )
        self.assertEqual([item.text for item in merged].count("好的"), 2)

    def test_perceptual_hash_tolerates_tiny_screen_change(self) -> None:
        first = Image.new("RGB", (200, 120), "white")
        second = first.copy()
        second.putpixel((10, 10), (245, 245, 245))
        self.assertLessEqual(_hash_distance(_perceptual_hash(first), _perceptual_hash(second)), 2)


class ConversationMemoryTests(unittest.TestCase):
    def test_database_message_id_is_primary_memory_anchor(self) -> None:
        message = ChatMessage(
            "示例成员甲",
            "同样的文字",
            0,
            1.0,
            message_id="12345:67",
        )
        self.assertEqual(message_anchor(message), "id:12345:67")

    def test_history_corrects_one_character_nickname_error(self) -> None:
        memory = [
            ChatMessage("陈晓宇", "第一句", 0, 0.99, speaker_confidence=0.99).to_dict(),
            ChatMessage("陈晓宇", "第二句", 1, 0.99, speaker_confidence=0.99).to_dict(),
        ]
        current = [ChatMessage("陈小宇", "新消息", 0, 0.99, speaker_confidence=0.8)]
        corrections = canonicalize_speakers(current, memory)
        self.assertEqual(current[0].speaker, "陈晓宇")
        self.assertEqual(current[0].sender_source, "memory_correction")
        self.assertEqual(corrections, ["陈小宇→陈晓宇"])

    def test_matching_memory_requires_two_recent_anchors(self) -> None:
        datetime_module = __import__("datetime")
        now = datetime_module.datetime.now(datetime_module.timezone.utc).isoformat()
        memory_messages = [
            ChatMessage("示例成员甲", "消息一", 0, 0.99).to_dict(),
            ChatMessage("示例成员乙", "消息二", 1, 0.99).to_dict(),
        ]
        current = [
            ChatMessage("未知群成员", "消息一", 0, 0.9),
            ChatMessage("未知群成员", "消息二", 1, 0.9),
            ChatMessage("示例成员甲", "新消息", 2, 0.99),
        ]
        match, overlap = find_matching_memory(
            current,
            [{
                "memory_id": "test",
                "updated_at": now,
                "parser_version": MEMORY_PARSER_VERSION,
                "messages": memory_messages,
            }],
        )
        self.assertIsNotNone(match)
        self.assertEqual(overlap, 2)

    def test_old_parser_memory_is_not_reused(self) -> None:
        datetime_module = __import__("datetime")
        now = datetime_module.datetime.now(datetime_module.timezone.utc).isoformat()
        current = [
            ChatMessage("未知群成员", "消息一", 0, 0.9),
            ChatMessage("未知群成员", "消息二", 1, 0.9),
        ]
        match, _ = find_matching_memory(
            current,
            [{
                "memory_id": "old",
                "updated_at": now,
                "messages": [message.to_dict() for message in current],
            }],
        )
        self.assertIsNone(match)

    def test_memory_merge_only_appends_new_tail(self) -> None:
        stored = [
            ChatMessage("示例成员甲", "旧消息", 0, 0.99).to_dict(),
            ChatMessage("示例成员乙", "共同消息一", 1, 0.99).to_dict(),
            ChatMessage("示例成员乙", "共同消息二", 2, 0.99).to_dict(),
        ]
        current = [
            ChatMessage("示例成员乙", "共同消息一", 0, 0.99),
            ChatMessage("示例成员乙", "共同消息二", 1, 0.99),
            ChatMessage("示例成员甲", "新消息", 2, 0.99),
        ]
        merged, added, _ = merge_with_memory({"messages": stored}, current)
        self.assertEqual(
            [message.text for message in merged],
            ["旧消息", "共同消息一", "共同消息二", "新消息"],
        )
        self.assertEqual(added, 1)


class TypeSafeClientTests(unittest.TestCase):
    def test_latest_self_reply_with_followup_is_scored(self) -> None:
        messages = [
            {"speaker": "对方", "text": "你记得吗", "is_self": False},
            {"speaker": "我方", "text": "我先查一下", "is_self": True},
            {"speaker": "对方", "text": "好，你查吧", "is_self": False},
        ]
        count, evaluation = build_reply_evaluation(messages)
        self.assertEqual(count, 1)
        self.assertEqual(evaluation["reply"]["text"], "我先查一下")
        questions = build_questions({"reply_evaluation": evaluation, "participants": []})
        self.assertIn("my_reply_relevance", questions)
        self.assertIn("my_reply_outcome", questions)
        self.assertIn("my_reply_effectiveness", questions)

    def test_self_reply_without_followup_is_not_scored(self) -> None:
        count, evaluation = build_reply_evaluation([
            {"speaker": "我方", "text": "等我确认", "is_self": True},
        ])
        self.assertEqual(count, 1)
        self.assertIsNone(evaluation)

    def test_large_group_limits_individual_questions_to_six_people(self) -> None:
        messages = [
            {"speaker": f"成员{index}", "text": "消息"}
            for index in range(20)
        ]
        participants, omitted = select_focus_participants(messages)
        self.assertEqual(len(participants), 6)
        self.assertEqual(omitted, 14)
        questions = build_questions({"participants": participants, "messages": messages})
        self.assertEqual(
            len([key for key in questions if key.startswith("person_intent_")]),
            6,
        )

    def test_state_limit_keeps_newest_messages(self) -> None:
        messages = [{"speaker": "对方", "text": str(index) * 50} for index in range(100)]
        selected = limit_messages_for_state(messages, max_chars=500)
        self.assertLess(len(selected), len(messages))
        self.assertEqual(selected[-1], messages[-1])

    @patch("wechat_jev.typesafe_client.requests.post")
    def test_successful_response(self, post: Mock) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {"model": "jev-test", "answers": {"literal_only": {"type": "noul", "noul": 0.5}}}
        response.raise_for_status.return_value = None
        post.return_value = response
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}):
            result = TypeSafeClient(timeout=1).evaluate({"messages": []})
        self.assertEqual(result.payload["model"], "jev-test")
        sent = post.call_args.kwargs["json"]
        self.assertTrue({
            "situation",
            "relationship_need",
            "cares_test",
            "concrete_reply_risk",
            "needs_repair_first",
            "conflict_emergency",
            "deescalated",
            "stop_explaining",
            "literal_only",
            "intent",
            "tension",
            "next_action",
        }.issubset(sent["questions"]))

    def test_context_cards_are_independent_typed_questions(self) -> None:
        questions = build_questions({"participants": [], "messages": [], "focus_message": {}})
        self.assertEqual(questions["situation"]["type"], "choice")
        self.assertEqual(questions["relationship_need"]["type"], "choice")
        for key in (
            "cares_test",
            "concrete_reply_risk",
            "needs_repair_first",
            "conflict_emergency",
            "deescalated",
            "stop_explaining",
        ):
            self.assertEqual(questions[key]["type"], "noul")

    def test_group_chat_builds_one_intent_question_per_person(self) -> None:
        state = {"participants": ["张三", "李四", "我方"], "messages": []}
        questions = build_questions(state)
        self.assertIn("person_intent_0", questions)
        self.assertIn("person_intent_1", questions)
        self.assertNotIn("person_intent_2", questions)
        self.assertIn("张三", questions["person_intent_0"]["instructions"])

    def test_one_named_group_member_also_gets_role_intent_question(self) -> None:
        questions = build_questions({
            "participants": ["张三"],
            "messages": [],
            "participant_context": {"张三": [{"text": "今晚吃什么"}]},
        })
        self.assertIn("person_intent_0", questions)
        self.assertIn("participant_context", questions["person_intent_0"]["instructions"])

    def test_unknown_member_does_not_get_person_question(self) -> None:
        questions = build_questions({"participants": ["未知群成员", "对方"], "messages": []})
        self.assertFalse(any(key.startswith("person_intent_") for key in questions))

    def test_missing_key_is_clear(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch("winreg.OpenKey", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(TypeSafeError, "TYPESAFE_API_KEY"):
                TypeSafeClient().evaluate({"messages": []})


class AnalysisStartTests(unittest.TestCase):
    @patch("wechat_jev.ui.threading.Thread")
    @patch("wechat_jev.ui.get_foreground_wechat")
    def test_wechat_is_locked_before_worker_starts(self, foreground: Mock, thread_class: Mock) -> None:
        from wechat_jev.ui import AssistantApp

        context = WindowContext(hwnd=123, rect=(0, 0, 1000, 800), title="测试会话")
        foreground.return_value = context
        app = AssistantApp.__new__(AssistantApp)
        app.config = AppConfig()
        app.worker_lock = __import__("threading").Lock()
        app.status_var = Mock()
        app.root = Mock()
        app._show_error = Mock()
        app._set_output = Mock()
        app.start_analysis()
        foreground.assert_called_once_with()
        thread_class.assert_called_once()
        self.assertEqual(thread_class.call_args.kwargs["args"], (context,))


class SpeakerColorTests(unittest.TestCase):
    def test_history_filters_are_optional_and_combined(self) -> None:
        from wechat_jev.ui import history_record_matches

        record = {
            "created_at": "2026-09-22T12:00:00",
            "contact": "测试学习群",
            "raw_messages": [
                {"speaker": "张三", "text": "你好"},
                {"speaker": "我方", "text": "收到"},
            ],
        }
        self.assertTrue(history_record_matches(record))
        self.assertTrue(history_record_matches(record, person="张"))
        self.assertTrue(history_record_matches(record, conversation="学习群"))
        self.assertTrue(history_record_matches(record, person="张山"))
        self.assertTrue(history_record_matches(record, conversation="测试学西群"))
        self.assertTrue(history_record_matches(record, date="2026-09-22"))
        self.assertTrue(history_record_matches(
            record, person="张三", conversation="测试", date="2026-09-22"
        ))
        self.assertFalse(history_record_matches(record, person="李四"))

    def test_private_history_person_uses_matched_contact_for_old_records(self) -> None:
        from wechat_jev.ui import history_record_matches

        record = {
            "created_at": "2026-09-22T12:00:00",
            "contact": "微信",
            "raw_messages": [{"speaker": "对方", "text": "你好"}],
            "redacted_state": {"conversation": {"type": "private"}},
            "warnings": ["数据库文字模式：OCR 识别“测试”，匹配“翟洪竣”，读取 100 条"],
        }
        self.assertTrue(history_record_matches(record, person="翟洪"))
        self.assertTrue(history_record_matches(record, conversation="翟洪竣"))

    def test_named_speakers_get_distinct_stable_colors(self) -> None:
        from wechat_jev.ui import build_speaker_colors

        messages = [
            {"speaker": "示例成员甲"},
            {"speaker": "我方"},
            {"speaker": "示例成员乙"},
            {"speaker": "示例成员甲"},
            {"speaker": "未知群成员"},
        ]
        colors = build_speaker_colors(messages)
        self.assertNotEqual(colors["示例成员甲"], colors["示例成员乙"])
        self.assertEqual(colors["我方"], "#07883D")
        self.assertEqual(colors["未知群成员"], "#616161")
        self.assertEqual(colors, build_speaker_colors(messages))


@unittest.skipUnless(sys.platform == "win32", "DPAPI 仅支持 Windows")
class EncryptedHistoryTests(unittest.TestCase):
    def test_round_trip_and_no_plaintext(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = EncryptedHistoryStore(Path(directory))
            store.add("宝儿", {"raw_messages": [{"text": "敏感聊天原文"}]})
            rows = store.list_recent()
            self.assertEqual(rows[0]["raw_messages"][0]["text"], "敏感聊天原文")
            db_bytes = (Path(directory) / "history.db").read_bytes()
            self.assertNotIn("敏感聊天原文".encode("utf-8"), db_bytes)

    def test_conversation_memory_is_encrypted_and_updateable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = EncryptedHistoryStore(Path(directory))
            store.upsert_memory("memory-1", "测试群", {"messages": [{"text": "记忆中的敏感消息"}]})
            store.upsert_memory("memory-1", "测试群", {"messages": [{"text": "更新后的敏感消息"}]})
            memories = store.list_memories()
            self.assertEqual(len(memories), 1)
            self.assertEqual(memories[0]["messages"][0]["text"], "更新后的敏感消息")
            db_bytes = (Path(directory) / "history.db").read_bytes()
            self.assertNotIn("更新后的敏感消息".encode("utf-8"), db_bytes)


if __name__ == "__main__":
    unittest.main()
