"""Terminal stop-reason extraction from agent_end frames."""
from __future__ import annotations

import unittest

from orches.omp import _terminal_stop_reason


class TerminalStopReasonTest(unittest.TestCase):
    def test_none_for_missing_frame(self) -> None:
        self.assertIsNone(_terminal_stop_reason(None))
        self.assertIsNone(_terminal_stop_reason({}))

    def test_top_level_wins(self) -> None:
        frame = {"stopReason": "aborted", "messages": [{"stopReason": "stop"}]}
        self.assertEqual(_terminal_stop_reason(frame), "aborted")

    def test_last_message_fallback(self) -> None:
        # Real omp agent_end frames carry stopReason only on messages.
        frame = {
            "type": "agent_end", "isTerminal": True, "yielded": True,
            "messages": [
                {"role": "user", "content": []},
                {"role": "assistant", "stopReason": "aborted"},
            ],
        }
        self.assertEqual(_terminal_stop_reason(frame), "aborted")

    def test_malformed_messages_ignored(self) -> None:
        self.assertIsNone(_terminal_stop_reason({"messages": []}))
        self.assertIsNone(_terminal_stop_reason({"messages": ["x"]}))
        self.assertIsNone(_terminal_stop_reason({"messages": [{"stopReason": 7}]}))


if __name__ == "__main__":
    unittest.main()
