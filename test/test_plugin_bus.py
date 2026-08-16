"""e2e FakeBus tests: list → launch round-trip and error paths."""
import textwrap
import tempfile
import os
import unittest
from unittest.mock import MagicMock, patch

from ovos_bus_client.message import Message
from ovos_utils.fakebus import FakeBus

from ovos_phal_plugin_app_launcher import AppLauncherPHALPlugin


def _make_plugin(extra_config=None):
    config = {
        "require_icon": False,
        "require_categories": False,
        "skip_categories": [],
        "match_threshold": 0.0,  # accept everything for test
    }
    if extra_config:
        config.update(extra_config)
    bus = FakeBus()
    # Patch the applist build to avoid hitting the real filesystem
    with patch.object(AppLauncherPHALPlugin, "_build_applist", return_value={
        "Firefox": "firefox",
        "Calc": "kcalc",
        "Terminal": "konsole",
    }):
        plugin = AppLauncherPHALPlugin(bus=bus, config=config)
    return plugin, bus


class TestListHandler(unittest.TestCase):

    def test_list_returns_apps(self):
        plugin, bus = _make_plugin()
        responses = []
        bus.on("ovos.phal.app_launcher.list.response", responses.append)
        msg = Message("ovos.phal.app_launcher.list")
        with patch.object(plugin, "_build_applist", return_value={"Firefox": "firefox"}):
            plugin.handle_list(msg)
        self.assertTrue(responses)
        apps = responses[0].data["apps"]
        self.assertIsInstance(apps, list)
        names = [a["name"] for a in apps]
        self.assertIn("Firefox", names)

    def test_list_response_structure(self):
        plugin, bus = _make_plugin()
        responses = []
        bus.on("ovos.phal.app_launcher.list.response", responses.append)
        with patch.object(plugin, "_build_applist", return_value={"VLC": "vlc"}):
            plugin.handle_list(Message("ovos.phal.app_launcher.list"))
        apps = responses[0].data["apps"]
        for app in apps:
            self.assertIn("name", app)
            self.assertIn("exec", app)


class TestLaunchHandler(unittest.TestCase):

    def test_launch_success(self):
        plugin, bus = _make_plugin()
        plugin._applist = {"Firefox": "firefox"}
        responses = []
        bus.on("ovos.phal.app_launcher.launch.response", responses.append)
        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value.poll.return_value = None  # still running
            plugin.handle_launch(Message("ovos.phal.app_launcher.launch", {"name": "Firefox"}))
        mock_popen.assert_called_once()
        self.assertTrue(responses[0].data["success"])
        self.assertEqual(responses[0].data["name"], "Firefox")

    def test_launch_immediate_nonzero_exit_is_not_success(self):
        plugin, bus = _make_plugin()
        plugin._applist = {"Firefox": "firefox"}
        responses = []
        bus.on("ovos.phal.app_launcher.launch.response", responses.append)
        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value.poll.return_value = 1  # already exited, error
            plugin.handle_launch(Message("ovos.phal.app_launcher.launch", {"name": "Firefox"}))
        self.assertFalse(responses[0].data.get("success", False))
        self.assertIn("error", responses[0].data)

    def test_launch_no_match(self):
        plugin, bus = _make_plugin({"match_threshold": 0.99})
        plugin._applist = {"Firefox": "firefox"}
        responses = []
        bus.on("ovos.phal.app_launcher.launch.response", responses.append)
        with patch("subprocess.Popen"):
            plugin.handle_launch(Message("ovos.phal.app_launcher.launch", {"name": "xyzzy_nonexistent_app"}))
        self.assertIn("error", responses[0].data)

    def test_launch_missing_name(self):
        plugin, bus = _make_plugin()
        responses = []
        bus.on("ovos.phal.app_launcher.launch.response", responses.append)
        plugin.handle_launch(Message("ovos.phal.app_launcher.launch", {}))
        self.assertIn("error", responses[0].data)

    def test_launch_subprocess_error(self):
        plugin, bus = _make_plugin()
        plugin._applist = {"Crash": "crash_app"}
        responses = []
        bus.on("ovos.phal.app_launcher.launch.response", responses.append)
        with patch("subprocess.Popen", side_effect=FileNotFoundError("crash_app not found")):
            plugin.handle_launch(Message("ovos.phal.app_launcher.launch", {"name": "Crash"}))
        self.assertIn("error", responses[0].data)
        self.assertFalse(responses[0].data.get("success", False))


class TestCloseHandler(unittest.TestCase):

    def test_close_no_match_returns_error(self):
        plugin, bus = _make_plugin()
        plugin._applist = {"Firefox": "firefox"}
        responses = []
        bus.on("ovos.phal.app_launcher.close.response", responses.append)
        with patch.object(plugin, "_close_by_process", return_value=False), \
             patch.object(plugin, "_close_by_window", return_value=False):
            plugin.handle_close(Message("ovos.phal.app_launcher.close", {"name": "Firefox"}))
        self.assertIn("error", responses[0].data)

    def test_close_success_via_process(self):
        plugin, bus = _make_plugin()
        plugin._applist = {"Firefox": "firefox"}
        responses = []
        bus.on("ovos.phal.app_launcher.close.response", responses.append)
        with patch.object(plugin, "_close_by_process", return_value=True):
            plugin.handle_close(Message("ovos.phal.app_launcher.close", {"name": "Firefox"}))
        self.assertTrue(responses[0].data["success"])


class TestIsRunningHandler(unittest.TestCase):

    def test_is_running_true(self):
        plugin, bus = _make_plugin()
        plugin._applist = {"Firefox": "firefox"}
        responses = []
        bus.on("ovos.phal.app_launcher.is_running.response", responses.append)
        with patch.object(plugin, "_is_running", return_value=True):
            plugin.handle_is_running(Message("ovos.phal.app_launcher.is_running", {"name": "Firefox"}))
        self.assertTrue(responses[0].data["running"])

    def test_is_running_false(self):
        plugin, bus = _make_plugin()
        plugin._applist = {"Firefox": "firefox"}
        responses = []
        bus.on("ovos.phal.app_launcher.is_running.response", responses.append)
        with patch.object(plugin, "_is_running", return_value=False):
            plugin.handle_is_running(Message("ovos.phal.app_launcher.is_running", {"name": "Firefox"}))
        self.assertFalse(responses[0].data["running"])

    def test_is_running_missing_name(self):
        plugin, bus = _make_plugin()
        responses = []
        bus.on("ovos.phal.app_launcher.is_running.response", responses.append)
        plugin.handle_is_running(Message("ovos.phal.app_launcher.is_running", {}))
        self.assertFalse(responses[0].data["running"])


class TestListLaunchRoundTrip(unittest.TestCase):
    """End-to-end: list apps then launch one."""

    def test_list_then_launch(self):
        plugin, bus = _make_plugin()
        applist = {"Firefox": "firefox", "VLC": "vlc"}
        plugin._applist = applist

        list_responses = []
        launch_responses = []
        bus.on("ovos.phal.app_launcher.list.response", list_responses.append)
        bus.on("ovos.phal.app_launcher.launch.response", launch_responses.append)

        with patch.object(plugin, "_build_applist", return_value=applist):
            plugin.handle_list(Message("ovos.phal.app_launcher.list"))

        names = [a["name"] for a in list_responses[0].data["apps"]]
        self.assertTrue(len(names) > 0)

        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value.poll.return_value = None  # still running
            plugin.handle_launch(Message("ovos.phal.app_launcher.launch", {"name": names[0]}))

        self.assertTrue(launch_responses[0].data.get("success"))


if __name__ == "__main__":
    unittest.main()
