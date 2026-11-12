"""Slow presentation subscribers never displace command replies or input receipts."""
import json
import socket
import threading
import unittest

import test_runtime  # noqa: F401 - resolve the live source packages
from kernelcat_worker.application_host import Connection


class TransportTests(unittest.TestCase):
    def connection(self):
        local, peer = socket.socketpair()
        local.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        peer.settimeout(5)
        connection = Connection(None, local)
        self.addCleanup(peer.close)
        self.addCleanup(local.close)
        self.addCleanup(connection.close)
        return connection, peer

    def test_coalescing_preserves_text_and_has_a_byte_ceiling(self):
        connection, _ = self.connection()
        fragment = "中文证据" * 20
        for sequence in range(1500):
            connection.emit({"type": "model_delta", "generation_id": "parent", "sequence": sequence,
                             "delta": {"type": "text_delta", "delta": fragment}})
        self.assertFalse(connection.closed.is_set())
        self.assertEqual("".join(event["delta"]["delta"] for event, _ in connection.observations), fragment * 1500)
        self.assertTrue(all(len(payload) <= 256 * 1024 for _, payload in connection.observations))
        self.assertLessEqual(connection.observation_bytes, 4 * 1024 * 1024)

    def test_slow_observer_cannot_block_control_or_drop_original_input(self):
        observer, observer_peer = self.connection()
        control, control_peer = self.connection()
        writers = [threading.Thread(target=connection.writer, daemon=True) for connection in (observer, control)]
        for writer in writers:
            writer.start()
        # Leave the observer socket unread while a burst exceeds both socket
        # capacity and the former shared 256-message response queue.
        for sequence in range(2000):
            observer.emit({"type": "model_delta", "generation_id": f"child-{sequence % 2}", "sequence": sequence,
                           "delta": {"type": "text_delta", "delta": "preview" * 1024}})
        receipt = {"input_id": "original-notification", "text": "[子代理结果] original", "status": "queued"}
        observer.emit({"type": "input", "receipt": receipt})
        complete = "COMPLETE ORIGINAL RESULT 中文\n" * 2000
        control.emit({"id": 7, "result": {"text": complete, "input_id": receipt["input_id"]}})
        with control_peer.makefile("rb") as reader:
            response = json.loads(reader.readline())
        self.assertEqual(response, {"id": 7, "result": {"text": complete, "input_id": receipt["input_id"]}})
        self.assertFalse(observer.closed.is_set())
        self.assertLessEqual(len(observer.observations), 256)
        self.assertLessEqual(observer.observation_bytes, 4 * 1024 * 1024)
        # Once the consumer resumes, its retained notification follows the
        # already-started preview frame. No new identity or submission is made.
        with observer_peer.makefile("rb") as reader:
            for _ in range(258):
                event = json.loads(reader.readline())
                if event.get("type") == "input":
                    self.assertEqual(event["receipt"], receipt)
                    break
            else:
                self.fail("original notification was displaced by preview traffic")
        for connection in (observer, control):
            connection.close()
        for writer in writers:
            writer.join(timeout=2)
            self.assertFalse(writer.is_alive())

    def test_latest_state_replaces_old_display_snapshot_without_displacing_reply(self):
        connection, _ = self.connection()
        for cursor in range(1000):
            connection.emit({"type": "state", "state": {"cursor": cursor}})
        connection.emit({"id": 1, "result": {"accepted": True}})
        self.assertEqual(len(connection.observations), 1)
        self.assertEqual(connection.observations[0][0]["state"]["cursor"], 999)
        self.assertEqual(len(connection.outgoing), 1)
        self.assertFalse(connection.closed.is_set())


if __name__ == "__main__":
    unittest.main()
