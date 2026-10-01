import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app import network


class DiscoveryTests(unittest.TestCase):
    def test_streams_candidates_before_all_networks_finish(self):
        release = threading.Event()
        delivered = threading.Event()
        candidates, probed = [], []

        def probe(host):
            probed.append(host)
            if host == "192.168.1.1":
                return host, "First printer"
            if not release.wait(3):
                raise TimeoutError("The test did not release the remaining scan")
            return None

        def found(candidate):
            candidates.append(candidate)
            delivered.set()

        with patch("app.network.probe", side_effect=probe), ThreadPoolExecutor(max_workers=1) as worker:
            future = worker.submit(network.discover, ["192.168.1.0/30", "192.168.2.0/30"], on_found=found)
            try:
                self.assertTrue(delivered.wait(2))
                self.assertFalse(future.done())
                self.assertEqual(candidates, [("192.168.1.1", "First printer")])
            finally:
                release.set()
            self.assertEqual(future.result(timeout=3), candidates)
        self.assertCountEqual(probed, ["192.168.1.1", "192.168.1.2", "192.168.2.1", "192.168.2.2"])

    def test_overlaps_and_point_to_point_networks_do_not_drop_or_repeat_hosts(self):
        probed = []
        with patch("app.network.probe", side_effect=lambda host: probed.append(host)):
            network.discover(["10.0.0.0/31", "10.0.0.2/31", "10.0.0.0/30", "10.0.0.1/32"])
        self.assertCountEqual(probed, ["10.0.0.0", "10.0.0.1", "10.0.0.2", "10.0.0.3"])

    def test_large_network_is_lazy_and_cancellable(self):
        stop = threading.Event()
        probed = []

        def probe(host):
            probed.append(host)
            stop.set()

        with patch("app.network.probe", side_effect=probe):
            network.discover(["10.0.0.0/8", "192.168.0.0/16"], stop=stop)
        self.assertGreater(len(probed), 0)
        self.assertLessEqual(len(probed), 48)


if __name__ == "__main__":
    unittest.main()
