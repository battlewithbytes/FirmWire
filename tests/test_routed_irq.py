import unittest

from firmwire.hw.routed_irq import RoutedLevelIRQController


class RoutedIRQTests(unittest.TestCase):
    def make(self, count=4, sources=8):
        self.edges = [[] for _ in range(count)]
        self.core = RoutedLevelIRQController(sources, [edge.append for edge in self.edges])
        return self.core

    def test_masked_pending_claim_and_device_clear_are_separate(self):
        core = self.make()
        core.configure(3, priority=5, targets=[2])
        core.set_level(3, True)
        self.assertIsNone(core.claim(2))
        core.set_mask(3, False)
        self.assertEqual(self.edges[2], [True])
        self.assertEqual(core.claim(2), 3)
        self.assertEqual(self.edges[2], [True, False])
        core.complete(2, 3)
        self.assertEqual(self.edges[2], [True, False, True])
        self.assertEqual(core.claim(2), 3)
        core.set_level(3, False)
        self.assertEqual(core.snapshot()["active"][2], [3])
        core.complete(2, 3)
        self.assertIsNone(core.claim(2))
        self.assertFalse(core.snapshot()["output_levels"][2])

    def test_nested_priority_and_lifo_completion(self):
        core = self.make(1)
        for source, priority in ((0, 40), (1, 20), (2, 40)):
            core.configure(source, priority=priority, targets=[0])
            core.set_mask(source, False)
        core.set_level(0, True)
        self.assertEqual(core.claim(0), 0)
        core.set_level(2, True)
        self.assertIsNone(core.claim(0))
        core.set_level(1, True)
        self.assertEqual(core.claim(0), 1)
        with self.assertRaises(ValueError): core.complete(0, 0)
        core.set_level(1, False)
        core.complete(0, 1)
        core.set_level(0, False)
        core.complete(0, 0)
        self.assertEqual(core.claim(0), 2)

    def test_routing_is_configurable_and_one_source_has_one_owner(self):
        for count in (1, 2, 4, 8):
            core = self.make(count)
            core.configure(0, priority=3, targets=range(count))
            core.set_mask(0, False)
            core.set_level(0, True)
            self.assertEqual(core.claim(0), 0)
            for output in range(count): self.assertIsNone(core.claim(output))
            core.configure(0, priority=2, targets=[count - 1])
            core.complete(0, 0)
            self.assertEqual(core.claim(count - 1), 0)

    def test_threshold_reroutes_pending_and_boundary_is_explicit(self):
        core = self.make(2)
        core.configure(1, priority=7, targets=[0, 1])
        core.set_mask(1, False)
        core.set_level(1, True)
        core.set_threshold(0, 7)
        self.assertIsNone(core.claim(0))
        self.assertEqual(core.claim(1), 1)

    def test_snapshot_is_passive_detached_and_no_duplicate_level_edges(self):
        core = self.make(1)
        core.configure(0, priority=0, targets=[0])
        core.set_mask(0, False)
        for _ in range(10): core.set_level(0, True)
        snapshot = core.snapshot()
        snapshot["pending"][0].clear()
        self.assertEqual(core.snapshot()["pending"], [[0]])
        self.assertEqual(self.edges, [[True]])
        self.assertFalse(snapshot["hardware_semantics_verified"])

    def test_invalid_configuration_does_not_change_state(self):
        core = self.make(2)
        before = core.snapshot()
        operations = [lambda: core.configure(0, priority=-1, targets=[0]),
                      lambda: core.configure(0, priority=1, targets=[0, 2]),
                      lambda: core.configure(0, priority=1, targets=[0, 0]),
                      lambda: core.set_level(True, True), lambda: core.set_mask(0, 1),
                      lambda: core.set_level(0, 1), lambda: core.set_threshold(0, 129),
                      lambda: core.claim(-1), lambda: core.complete(0, 0)]
        for operation in operations:
            with self.assertRaises(ValueError): operation()
            self.assertEqual(core.snapshot(), before)


if __name__ == "__main__": unittest.main()
