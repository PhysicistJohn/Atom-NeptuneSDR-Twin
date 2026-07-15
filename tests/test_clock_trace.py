import tempfile
import unittest
from pathlib import Path

from neptunesdr_twin.clock import VirtualClock
from neptunesdr_twin.trace import TraceLog


class VirtualClockTests(unittest.TestCase):
    def test_equal_deadline_is_fifo_and_cancel_is_stable(self):
        clock = VirtualClock(10)
        observed = []
        clock.schedule(5, lambda: observed.append("first"), "first")
        cancelled = clock.schedule(5, lambda: observed.append("cancelled"))
        clock.schedule(5, lambda: observed.append("third"), "third")
        cancelled.cancel()
        self.assertEqual(clock.advance(5), 2)
        self.assertEqual(observed, ["first", "third"])
        self.assertEqual(clock.now_ns, 15)

    def test_callback_can_schedule_at_current_time(self):
        clock = VirtualClock()
        observed = []

        def parent():
            observed.append("parent")
            clock.schedule(0, lambda: observed.append("child"))

        clock.schedule(2, parent)
        clock.advance(2)
        self.assertEqual(observed, ["parent", "child"])


class TraceTests(unittest.TestCase):
    def test_trace_is_canonical_and_content_addressed(self):
        trace = TraceLog()
        trace.append(
            logical_ns=4,
            contact="usb.ep0",
            direction="host->device",
            event="setup",
            payload={"b": 2, "a": 1},
        )
        digest = trace.sha256()
        self.assertEqual(len(digest), 64)
        with tempfile.TemporaryDirectory() as directory:
            written = trace.write_jsonl(Path(directory) / "trace.jsonl")
            self.assertEqual(written, digest)


if __name__ == "__main__":
    unittest.main()
