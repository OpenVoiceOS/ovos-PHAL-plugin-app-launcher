"""Regression tests: every bus request must get exactly one response, even
when psutil.process_iter() races against short-lived processes exiting
mid-iteration (NoSuchProcess / AccessDenied), and wmctrl hangs must not
block a request forever.
"""
import subprocess
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import psutil
from ovos_bus_client.message import Message
from ovos_utils.fakebus import FakeBus

from ovos_phal_plugin_app_launcher import AppLauncherPHALPlugin


def _make_plugin():
    config = {
        "require_icon": False,
        "require_categories": False,
        "skip_categories": [],
        "match_threshold": 0.0,
        "disable_window_manager": True,
    }
    bus = FakeBus()
    with patch.object(AppLauncherPHALPlugin, "_build_applist", return_value={"True": "true"}):
        plugin = AppLauncherPHALPlugin(bus=bus, config=config)
    return plugin, bus


class TestProcessChurnNeverDropsResponse(unittest.TestCase):
    """Drives is_running requests against real /bin/true process churn:
    process_iter() snapshots PIDs, so a process exiting between the
    snapshot and proc.status()/proc.info access must not escape and
    silently drop the caller's response.
    """

    N_REQUESTS = 300

    def test_is_running_always_responds_under_process_churn(self):
        plugin, bus = _make_plugin()
        plugin._applist = {"True": "true"}

        responses = []
        bus.on("ovos.phal.app_launcher.is_running.response", responses.append)

        stop = threading.Event()

        def churn():
            while not stop.is_set():
                try:
                    subprocess.Popen(["/bin/true"]).wait()
                except Exception:
                    pass

        threads = [threading.Thread(target=churn, daemon=True) for _ in range(4)]
        for t in threads:
            t.start()
        try:
            for _ in range(self.N_REQUESTS):
                plugin.handle_is_running(Message("ovos.phal.app_launcher.is_running", {"name": "True"}))
        finally:
            stop.set()
            for t in threads:
                t.join(timeout=2)

        dropped = self.N_REQUESTS - len(responses)
        self.assertEqual(
            dropped, 0,
            f"{dropped}/{self.N_REQUESTS} is_running requests never got a response",
        )
        # A psutil.Error guard that has been gutted turns real races into
        # dropped-response-shaped error replies instead of a true
        # running/not-running answer - psutil's exception message never
        # contains the class name "NoSuchProcess", so asserting on that
        # string is dead code. Assert the response shape directly instead.
        for resp in responses:
            self.assertNotIn("error", resp.data)


class TestCloseErrorResponseShape(unittest.TestCase):
    def test_close_reports_psutil_race_as_error_not_exception(self):
        plugin, bus = _make_plugin()
        plugin._applist = {"True": "true"}
        responses = []
        bus.on("ovos.phal.app_launcher.close.response", responses.append)

        with patch.object(plugin, "_close_by_window", return_value=False), \
             patch.object(plugin, "_close_by_process", side_effect=psutil.NoSuchProcess(1234)):
            plugin.handle_close(Message("ovos.phal.app_launcher.close", {"name": "True"}))

        self.assertEqual(len(responses), 1)
        self.assertIn("error", responses[0].data)
        self.assertFalse(responses[0].data.get("success", False))


class TestIsRunningErrorResponseShape(unittest.TestCase):
    def test_is_running_reports_psutil_race_as_error_not_exception(self):
        plugin, bus = _make_plugin()
        plugin._applist = {"True": "true"}
        responses = []
        bus.on("ovos.phal.app_launcher.is_running.response", responses.append)

        with patch.object(plugin, "_is_running", side_effect=psutil.AccessDenied(1234)):
            plugin.handle_is_running(Message("ovos.phal.app_launcher.is_running", {"name": "True"}))

        self.assertEqual(len(responses), 1)
        self.assertIn("error", responses[0].data)


class TestWmctrlTimeout(unittest.TestCase):
    def test_wmctrl_timeout_yields_no_mapping_not_exception(self):
        plugin, bus = _make_plugin()
        plugin._wmctrl = "wmctrl"

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="wmctrl", timeout=3)):
            windows = plugin._get_window_process_mapping()

        self.assertEqual(windows, [])

    def test_hung_wmctrl_close_does_not_block_handle_close_forever(self):
        """A wmctrl invoked with `-ic` that hangs (e.g. an unresponsive
        window manager) must not block handle_close indefinitely - it has
        the same timeout as the `-lp` enumeration call.
        """
        plugin, bus = _make_plugin()
        plugin._wmctrl = "wmctrl"
        plugin.config["disable_window_manager"] = False

        responses = []
        bus.on("ovos.phal.app_launcher.close.response", responses.append)

        fake_window = ("0x01", MagicMock(), 0.0, "Firefox")

        def fake_run(cmd, *args, **kwargs):
            if cmd[1] == "-ic":
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            return result

        with patch.object(plugin, "_match_window", return_value=[fake_window]), \
             patch("subprocess.run", side_effect=fake_run) as mock_run, \
             patch.object(plugin, "_close_by_process", return_value=False):
            start = time.time()
            plugin.handle_close(Message("ovos.phal.app_launcher.close", {"name": "Firefox"}))
            elapsed = time.time() - start

        self.assertLess(elapsed, 4, "handle_close blocked past the wmctrl timeout")
        self.assertEqual(len(responses), 1)
        # every -ic call must have been given a timeout, same as -lp
        for call in mock_run.call_args_list:
            if call.args[0][1] == "-ic":
                self.assertIn("timeout", call.kwargs)


class TestNonStringNamePayloads(unittest.TestCase):
    """Fuzz matrix: garbage `name` payloads must never drop the response or
    let an exception escape any of the three name-taking handlers.
    """

    PAYLOADS = [123, True, False, None, [], {}, ["firefox"], {"name": "firefox"}]

    def test_non_string_name_never_drops_response_or_raises(self):
        plugin, bus = _make_plugin()
        plugin._applist = {"Firefox": "firefox"}

        handlers = {
            "ovos.phal.app_launcher.launch": plugin.handle_launch,
            "ovos.phal.app_launcher.close": plugin.handle_close,
            "ovos.phal.app_launcher.is_running": plugin.handle_is_running,
        }

        for event, handler in handlers.items():
            for payload in self.PAYLOADS:
                responses = []
                unsub_event = f"{event}.response"
                bus.on(unsub_event, responses.append)
                try:
                    with patch("subprocess.Popen"), \
                         patch.object(plugin, "_close_by_window", return_value=False), \
                         patch.object(plugin, "_close_by_process", return_value=False):
                        handler(Message(event, {"name": payload}))
                except Exception as exc:  # pragma: no cover - failure path
                    self.fail(f"{event} raised {exc!r} for payload {payload!r}")
                finally:
                    bus.remove(unsub_event, responses.append)
                self.assertEqual(
                    len(responses), 1,
                    f"{event} produced {len(responses)} responses for payload {payload!r}",
                )


class TestMatchProcessThreshold(unittest.TestCase):
    """A garbage name must never resolve to the best-scoring real process -
    it must be gated on the same match_threshold handle_launch uses.
    """

    def test_garbage_name_below_threshold_does_not_match_or_terminate(self):
        config = {
            "require_icon": False,
            "require_categories": False,
            "skip_categories": [],
            "match_threshold": 0.85,
            "disable_window_manager": True,
        }
        bus = FakeBus()
        with patch.object(AppLauncherPHALPlugin, "_build_applist", return_value={"Firefox": "firefox"}):
            plugin = AppLauncherPHALPlugin(bus=bus, config=config)
        plugin._applist = {"Firefox": "firefox"}

        # is_running must report False - no fuzzy-scored process is close
        # enough to a nonsense name to count as a match.
        self.assertFalse(plugin._is_running("zzzznotanapp"))

        # close must not call terminate on any real process.
        with patch("psutil.Process.terminate") as mock_terminate:
            responses = []
            bus.on("ovos.phal.app_launcher.close.response", responses.append)
            plugin.handle_close(Message("ovos.phal.app_launcher.close", {"name": "zzzznotanapp"}))
        mock_terminate.assert_not_called()
        self.assertEqual(len(responses), 1)
        self.assertIn("error", responses[0].data)


if __name__ == "__main__":
    unittest.main()
