import pickle
import struct
import unittest

from firmwire.vendor.mtk.hw.ccci_mailbox import (
    MailboxMessage, MailboxResult, MailboxEndpoint, MailboxDispatcher, decode_mailbox,
)


class CaptureEndpoint(MailboxEndpoint):
    def __init__(self, response=None):
        self.messages = []
        self.response = response

    def receive(self, message):
        self.messages.append(message)
        return MailboxResult("captured-test-message", self.response)


class MailboxTests(unittest.TestCase):
    def test_round_trip_boundaries_and_private_parameter(self):
        for channel in (0, 2, 65535):
            for command in (0, 0x105, 0xffffffff):
                message = MailboxMessage(channel, command, 0xdeadbeef, 65535)
                self.assertEqual(decode_mailbox(message.encode()), message)
                self.assertEqual(len(message.encode()), 16)
                self.assertNotIn(str(0xdeadbeef), repr(message))
                self.assertNotIn("deadbeef", repr(message))

    def test_invalid_fields_fail_before_serialization(self):
        for index, bound in ((0, 65536), (1, 1 << 32), (2, 1 << 32), (3, 65536)):
            for bad in (-1, bound, True, 1.5, "1", None):
                values = [2, 7, 0, 0]
                values[index] = bad
                with self.assertRaises(ValueError): MailboxMessage(*values)

    def test_malformed_envelopes_never_reach_endpoint(self):
        endpoint = CaptureEndpoint()
        dispatcher = MailboxDispatcher([(2, 7, endpoint)])
        for size in list(range(16)) + [17, 32, 1024]:
            with self.assertRaises(ValueError): dispatcher.receive(bytes(size))
        for magic in (0, 1, 0xfffffffe):
            with self.assertRaises(ValueError):
                dispatcher.receive(struct.pack("<IIHHI", magic, 7, 2, 0, 0))
        self.assertEqual(endpoint.messages, [])

    def test_exact_routes_no_fallback_and_no_automatic_reply(self):
        first, second = CaptureEndpoint(), CaptureEndpoint()
        dispatcher = MailboxDispatcher([(2, 7, first), (72, 7, second), (2, 8, second)])
        for channel, command in ((2, 9), (71, 7), (72, 8)):
            with self.assertRaises(NotImplementedError):
                dispatcher.receive(MailboxMessage(channel, command).encode())
        request = MailboxMessage(2, 7, 0x1234, 0x8123)
        self.assertIsNone(dispatcher.receive(request.encode()).response)
        self.assertEqual(first.messages, [request])
        self.assertEqual(second.messages, [])
        self.assertTrue(dispatcher.accepts(72))
        self.assertFalse(dispatcher.accepts(71))
        with self.assertRaises(NotImplementedError):
            MailboxDispatcher([]).receive(request.encode())

    def test_reply_is_explicit_and_not_an_echo_of_request(self):
        reply = MailboxMessage(73, 101, 42, 0)
        endpoint = CaptureEndpoint(reply)
        dispatcher = MailboxDispatcher([(72, 100, endpoint)])
        result = dispatcher.receive(MailboxMessage(72, 100, 0xdeadbeef, 0xffff).encode())
        self.assertEqual(result.response, reply)
        self.assertEqual(pickle.loads(pickle.dumps(dispatcher)).receive(
            MailboxMessage(72, 100).encode()), result)

    def test_registration_is_copied_and_duplicates_rejected(self):
        endpoint = CaptureEndpoint()
        routes = [(2, 7, endpoint)]
        dispatcher = MailboxDispatcher(routes)
        routes.append((3, 7, endpoint))
        self.assertFalse(dispatcher.accepts(3))
        dispatcher.validate_channels((0, 14, 32, 34))
        with self.assertRaises(ValueError): dispatcher.validate_channels((2,))
        for invalid in ([(2, 7, endpoint)] * 2, [(2, 7, object())],
                        [(True, 7, endpoint)], [(2, -1, endpoint)], [(65536, 7, endpoint)]):
            with self.assertRaises(ValueError): MailboxDispatcher(invalid)

    def test_handler_contract_failures_are_not_success(self):
        class BadEndpoint(MailboxEndpoint):
            def receive(self, message):
                return None
        with self.assertRaises(TypeError):
            MailboxDispatcher([(2, 7, BadEndpoint())]).receive(MailboxMessage(2, 7).encode())
        for disposition in (None, "", " "):
            with self.assertRaises(ValueError): MailboxResult(disposition)
        with self.assertRaises(ValueError): MailboxResult("bad", b"reply")


if __name__ == "__main__":
    unittest.main()
