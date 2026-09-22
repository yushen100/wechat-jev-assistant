from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

import requests

from wechat_jev.typesafe_client import TypeSafeClient, compact_state_for_retry


class TypeSafeTimeoutTests(unittest.TestCase):
    def test_compact_state_limits_messages_people_and_context(self) -> None:
        messages = [
            {"speaker": f"成员{index % 6}", "text": f"消息{index}"}
            for index in range(100)
        ]
        state = {
            "messages": messages,
            "participants": [f"成员{index}" for index in range(6)],
            "participant_context": {
                f"成员{index}": messages[index::6]
                for index in range(6)
            },
        }
        compact = compact_state_for_retry(state)
        self.assertEqual(len(compact["messages"]), 40)
        self.assertEqual(len(compact["participants"]), 3)
        self.assertTrue(all(len(value) <= 4 for value in compact["participant_context"].values()))

    @patch("wechat_jev.typesafe_client.time.sleep")
    @patch("wechat_jev.typesafe_client.requests.post")
    def test_read_timeout_retries_with_compact_state(self, post: Mock, _sleep: Mock) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {"answers": {}}
        response.raise_for_status.return_value = None
        post.side_effect = [requests.ReadTimeout(), response]
        state = {
            "messages": [{"speaker": "对方", "text": str(index)} for index in range(100)],
            "participants": [f"成员{index}" for index in range(6)],
            "participant_context": {},
        }
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}):
            result = TypeSafeClient(timeout=1).evaluate(state)
        self.assertTrue(result.fallback_used)
        retry_state = post.call_args_list[1].kwargs["json"]["state"]
        self.assertEqual(len(retry_state["messages"]), 40)
        self.assertEqual(len(retry_state["participants"]), 3)


if __name__ == "__main__":
    unittest.main()
