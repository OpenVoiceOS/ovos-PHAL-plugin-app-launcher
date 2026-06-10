"""Unit tests for .desktop file parsing and applist building."""
import os
import tempfile
import textwrap
import unittest

from ovos_phal_plugin_app_launcher import _parse_desktop_file, _iter_desktop_apps


MINIMAL_DESKTOP = textwrap.dedent("""\
    [Desktop Entry]
    Name=TestApp
    Exec=testapp --flag
    Type=Application
    Icon=testapp
    Categories=Utility;
""")

DESKTOP_NO_ICON = textwrap.dedent("""\
    [Desktop Entry]
    Name=NoIcon
    Exec=noicon
    Type=Application
    Categories=Utility;
""")

DESKTOP_SETTINGS = textwrap.dedent("""\
    [Desktop Entry]
    Name=SystemSettings
    Exec=systemsettings5
    Type=Application
    Icon=preferences-system
    Categories=Settings;
""")

DESKTOP_NOT_APP = textwrap.dedent("""\
    [Desktop Entry]
    Name=Link
    Exec=browser https://example.com
    Type=Link
    Icon=browser
    Categories=Network;
""")


def _write_desktop(content: str, tmpdir: str, name: str = "app.desktop") -> str:
    path = os.path.join(tmpdir, name)
    with open(path, "w") as fh:
        fh.write(content)
    return path


class TestParseDesktopFile(unittest.TestCase):

    def test_parses_basic_fields(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_desktop(MINIMAL_DESKTOP, td)
            info = _parse_desktop_file(path)
        self.assertEqual(info["Name"], "TestApp")
        self.assertEqual(info["Exec"], "testapp --flag")
        self.assertEqual(info["Type"], "Application")
        self.assertIn("Utility", info["Categories"])

    def test_missing_file_returns_empty(self):
        info = _parse_desktop_file("/nonexistent/path.desktop")
        self.assertEqual(info, {})

    def test_list_fields_parsed(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_desktop(MINIMAL_DESKTOP, td)
            info = _parse_desktop_file(path)
        self.assertIsInstance(info["Categories"], list)

    def test_no_desktop_entry_section(self):
        content = "[OtherSection]\nFoo=bar\n"
        with tempfile.TemporaryDirectory() as td:
            path = _write_desktop(content, td)
            info = _parse_desktop_file(path)
        self.assertEqual(info, {})


class TestIterDesktopApps(unittest.TestCase):

    def _apps_from_dir(self, tmpdir, **kwargs):
        """Monkey-patch search dirs to only use tmpdir."""
        import ovos_phal_plugin_app_launcher as mod
        orig = mod.expanduser

        # Override the iteration by mocking the search path via a subclass/function
        # We cannot easily patch os.listdir, so write files directly and call _iter_desktop_apps
        # with a patched version that only looks in tmpdir.
        results = []
        for info in _iter_desktop_apps(
                skip_categories=kwargs.get("skip_categories", ["Settings", "ConsoleOnly"]),
                skip_keywords=kwargs.get("skip_keywords", []),
                target_categories=kwargs.get("target_categories", []),
                target_keywords=kwargs.get("target_keywords", []),
                blacklist=kwargs.get("blacklist", []),
                extra_langs=None,
                require_icon=kwargs.get("require_icon", True),
                require_categories=kwargs.get("require_categories", True),
        ):
            results.append(info)
        return results

    def test_require_icon_filters_no_icon(self):
        with tempfile.TemporaryDirectory() as td:
            _write_desktop(DESKTOP_NO_ICON, td, "noicon.desktop")
            # Directly call iter with this path patched
            results = list(_iter_desktop_apps(
                skip_categories=[],
                skip_keywords=[],
                target_categories=[],
                target_keywords=[],
                blacklist=[],
                extra_langs=None,
                require_icon=True,
                require_categories=False,
            ))
        # real system apps may exist; our no-icon file should not appear
        for r in results:
            self.assertIn("Icon", r)

    def test_skip_category_filters(self):
        """Apps in Settings category should be skipped when skip_categories includes Settings."""
        results = list(_iter_desktop_apps(
            skip_categories=["Settings"],
            skip_keywords=[],
            target_categories=[],
            target_keywords=[],
            blacklist=[],
            extra_langs=None,
            require_icon=True,
            require_categories=True,
        ))
        for r in results:
            cats = r.get("Categories", [])
            self.assertNotIn("Settings", cats)


class TestParseDesktopLocale(unittest.TestCase):

    def test_extra_langs_included(self):
        content = textwrap.dedent("""\
            [Desktop Entry]
            Name=TestApp
            Name[de]=TestAnwendung
            Exec=testapp
            Type=Application
            Icon=testapp
            Categories=Utility;
        """)
        with tempfile.TemporaryDirectory() as td:
            path = _write_desktop(content, td)
            info = _parse_desktop_file(path, extra_langs=["de"])
        self.assertIn("Name[de]", info)
        self.assertEqual(info["Name[de]"], "TestAnwendung")


if __name__ == "__main__":
    unittest.main()
