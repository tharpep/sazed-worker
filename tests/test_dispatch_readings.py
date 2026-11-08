"""Dispatch mirrors plan window readings from the agent's stream, once per change."""

import unittest

from sazed_worker.dispatch.run_agent import ReadingSender


class FakeMirror:
    def __init__(self):
        self.readings = []

    def reading(self, info):
        self.readings.append(info)


def event(five, seven=0.8):
    windows = {
        "five_hour": {"utilization": five, "resetsAt": 1794103800},
        "seven_day": {"utilization": seven, "resetsAt": 1794312000},
    }
    return {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": windows}}


class Readings(unittest.TestCase):
    def test_posts_only_when_a_window_moves(self):
        mirror = FakeMirror()
        sender = ReadingSender(mirror)
        for e in (event(0.5), event(0.5), event(0.52), event(0.52, 0.81)):
            sender.add(e)
        self.assertEqual(len(mirror.readings), 3)

    def test_ignores_other_events_and_bad_shapes(self):
        mirror = FakeMirror()
        sender = ReadingSender(mirror)
        sender.add({"type": "assistant", "message": {}})
        sender.add({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": [1]}})
        sender.add({"type": "rate_limit_event"})
        self.assertEqual(mirror.readings, [])

    def test_does_nothing_without_a_mirror(self):
        ReadingSender(None).add(event(0.5))


if __name__ == "__main__":
    unittest.main()
