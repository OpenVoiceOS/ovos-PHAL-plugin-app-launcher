# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""ovos-PHAL-plugin-app-launcher

PHAL plugin that handles OS-level desktop application management on behalf of
ovos-skill-application-launcher (or any other bus client).  The plugin lives on
the *device* while the skill can live on a remote ovos-core node and still
control apps through HiveMind.

Bus event API
=============

All request/response pairs use the standard ``message.response()`` convention so
``bus.wait_for_response`` works transparently across HiveMind sessions.

+------------------------------------------------+---------------------+-----------------------------------------------+
| Request event                                  | Request payload     | Response event / payload                     |
+================================================+=====================+===============================================+
| ``ovos.phal.app_launcher.list``               | *(empty)*           | ``.response`` ``{apps: [{name, exec,         |
|                                                |                     | icon?, categories?}]}``                      |
+------------------------------------------------+---------------------+-----------------------------------------------+
| ``ovos.phal.app_launcher.launch``             | ``{name: str}``     | ``.response`` ``{name, success: true}``       |
|                                                |                     | or ``{name, error: str}``                    |
+------------------------------------------------+---------------------+-----------------------------------------------+
| ``ovos.phal.app_launcher.close``              | ``{name: str}``     | ``.response`` ``{name, success: true}``       |
|                                                |                     | or ``{name, error: str}``                    |
+------------------------------------------------+---------------------+-----------------------------------------------+
| ``ovos.phal.app_launcher.is_running``         | ``{name: str}``     | ``.response`` ``{name, running: bool}``      |
+------------------------------------------------+---------------------+-----------------------------------------------+

Configuration keys (ovos.conf / settings)
==========================================

``skip_categories``   list[str]   Desktop categories to exclude.
                                  Default: ``["Settings", "ConsoleOnly", "Building"]``
``skip_keywords``     list[str]   Desktop keywords to exclude.  Default: ``[]``
``target_categories`` list[str]   If non-empty, only include apps in these categories.
``target_keywords``   list[str]   If non-empty, only include apps with these keywords.
``blacklist``         list[str]   App names or ``.desktop`` filenames to exclude.
``require_icon``      bool        Skip apps without an ``Icon`` field.  Default: ``True``
``require_categories``bool        Skip apps without a ``Categories`` field.  Default: ``True``
``aliases``           dict        Map desktop name → list of speech-friendly aliases.
``user_commands``     dict        Map voice name → shell command.
``shell``             bool        Pass ``shell=True`` to ``subprocess.Popen``.  Default: ``False``
``terminate_all``     bool        Kill every matching process, not just the first.
``match_threshold``   float       Fuzzy-match threshold for app names.  Default: ``0.85``
"""
from __future__ import annotations

import configparser
import shlex
import subprocess
from os import listdir
from os.path import expanduser, isdir, join
from shutil import which
from typing import Dict, Generator, Iterable, List, Optional, Union

import psutil
from ovos_bus_client import Message
from ovos_plugin_manager.phal import PHALPlugin
from ovos_utils.log import LOG
from ovos_utils.parse import fuzzy_match, match_one

# ---------------------------------------------------------------------------
# Type alias
# ---------------------------------------------------------------------------
WindowMatch = tuple  # (window_id: str, process: psutil.Process, ctime: float, title: str)


# ---------------------------------------------------------------------------
# Desktop-file helpers  (static, no skill dependency)
# ---------------------------------------------------------------------------

def _parse_desktop_file(
        file_path: str,
        extra_langs: Optional[List[str]] = None,
) -> Dict[str, Union[str, List[str]]]:
    """Parse a ``.desktop`` file and return the fields we care about."""
    extra_langs = extra_langs or []

    config = configparser.ConfigParser(interpolation=None, delimiters=("=", ":"))
    config.optionxform = str  # preserve key case
    config.read(file_path)

    data: Dict[str, Union[str, List[str]]] = {}
    LIST_KEYS = {"Categories", "Keywords", "MimeType"}
    LIST_DELIM = ";"

    if "Desktop Entry" not in config:
        return data

    for key in config["Desktop Entry"].keys():
        v: Union[str, List[str]] = config["Desktop Entry"].get(key, "")
        base_key = key.split("[")[0]
        if base_key in LIST_KEYS:
            v = [x for x in v.split(LIST_DELIM) if x]
        # normalise locale suffixes, e.g. Name[de_DE] → keep as-is for now
        data[key] = v

    keys_of_interest = {
        "Name", "GenericName", "Categories", "Comment", "Keywords",
        "Exec", "Type", "Icon",
    }
    for lang in extra_langs:
        keys_of_interest |= {f"Name[{lang}]", f"GenericName[{lang}]", f"Comment[{lang}]"}

    return {k: v for k, v in data.items() if k.split("[")[0] in keys_of_interest or k in keys_of_interest}


def _iter_desktop_apps(
        skip_categories: List[str],
        skip_keywords: List[str],
        target_categories: List[str],
        target_keywords: List[str],
        blacklist: List[str],
        extra_langs: Optional[List[str]],
        require_icon: bool,
        require_categories: bool,
) -> Generator[Dict[str, Union[str, List[str]]], None, None]:
    """Yield parsed desktop-entry dicts that pass all filters."""
    search_dirs = [
        "/usr/share/applications/",
        "/usr/local/share/applications/",
        expanduser("~/.local/share/applications/"),
    ]
    for base in search_dirs:
        if not isdir(base):
            continue
        for fname in listdir(base):
            if not fname.endswith(".desktop") or fname in blacklist:
                continue
            info = _parse_desktop_file(join(base, fname), extra_langs=extra_langs)
            if not info:
                continue
            if "Exec" not in info:
                continue
            if info.get("Name") in blacklist:
                continue
            if info.get("Type") != "Application":
                continue
            if "Icon" not in info and require_icon:
                continue
            if "Categories" not in info and (target_categories or require_categories):
                continue
            if "Keywords" not in info and target_keywords:
                continue
            cats = info.get("Categories", [])
            kws = info.get("Keywords", [])
            if skip_categories and any(c in skip_categories for c in cats):
                continue
            if skip_keywords and any(k in skip_keywords for k in kws):
                continue
            if target_categories and not any(c in target_categories for c in cats):
                continue
            if target_keywords and not any(k in target_keywords for k in kws):
                continue
            yield info


# ---------------------------------------------------------------------------
# PHAL plugin
# ---------------------------------------------------------------------------

class AppLauncherPHALPlugin(PHALPlugin):
    """OS-side PHAL plugin for desktop application management.

    Listens for ``ovos.phal.app_launcher.*`` messages and performs the
    actual subprocess/process management, keeping the skill side free of any
    OS coupling.
    """

    def __init__(self, bus=None, config: Optional[Dict] = None) -> None:
        super().__init__(bus=bus, name="ovos-phal-plugin-app-launcher", config=config or {})
        self._applist: Optional[Dict[str, str]] = None  # lazy, rebuilt on list request
        self._wmctrl: Optional[str] = None
        if not self.config.get("disable_window_manager", False):
            self._wmctrl = which("wmctrl")
            if not self._wmctrl:
                LOG.warning("[app-launcher] 'wmctrl' not available; window management disabled")

        self.bus.on("ovos.phal.app_launcher.list", self.handle_list)
        self.bus.on("ovos.phal.app_launcher.launch", self.handle_launch)
        self.bus.on("ovos.phal.app_launcher.close", self.handle_close)
        self.bus.on("ovos.phal.app_launcher.is_running", self.handle_is_running)
        LOG.info("[app-launcher] PHAL plugin loaded")

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _cfg(self, key: str, default=None):
        return self.config.get(key, default)

    @property
    def _thresh(self) -> float:
        return float(self._cfg("match_threshold", 0.85))

    @property
    def applist(self) -> Dict[str, str]:
        """Lazily built map of spoken name → executable command."""
        if self._applist is None:
            self._applist = self._build_applist()
        return self._applist

    def _build_applist(self) -> Dict[str, str]:
        apps: Dict[str, str] = dict(self._cfg("user_commands") or {})
        norm = lambda k: k.replace(".desktop", "").replace("-", " ").replace("_", " ").split(".")[-1].title()
        aliases_map: Dict[str, List[str]] = self._cfg("aliases") or {"kcalc": ["calculator"]}

        for app in _iter_desktop_apps(
                skip_categories=self._cfg("skip_categories", ["Settings", "ConsoleOnly", "Building"]),
                skip_keywords=self._cfg("skip_keywords", []),
                target_categories=self._cfg("target_categories", []),
                target_keywords=self._cfg("target_keywords", []),
                blacklist=self._cfg("blacklist", []),
                extra_langs=None,
                require_icon=self._cfg("require_icon", True),
                require_categories=self._cfg("require_categories", True),
        ):
            cmd = app["Exec"].split(" ")[0].split("/")[-1].split(".")[0]
            names = [cmd]
            for k, v in app.items():
                if k.startswith("Name"):
                    names.append(v)
            names += [norm(n) for n in names]

            for name in set(names):
                if isinstance(name, str) and 3 <= len(name) <= 20:
                    apps[name] = cmd
                if name in aliases_map:
                    for alias in aliases_map[name]:
                        apps[alias] = cmd
                if isinstance(name, str) and name.startswith("K") and "KDE" in app.get("Categories", []):
                    alias = "C" + name[1:]
                    if alias not in apps:
                        apps[alias] = cmd

        LOG.debug(f"[app-launcher] built applist with {len(apps)} entries")
        return apps

    # ------------------------------------------------------------------
    # Bus handlers
    # ------------------------------------------------------------------

    def handle_list(self, message: Message) -> None:
        """Reply with all known launchable applications.

        Response payload::

            {
                "apps": [
                    {"name": "Firefox", "exec": "firefox",
                     "icon": "firefox", "categories": ["Network", "WebBrowser"]},
                    ...
                ]
            }
        """
        # Rebuild applist on each list request so newly-installed apps appear.
        self._applist = self._build_applist()
        apps = []
        seen_execs: set = set()
        for name, cmd in self.applist.items():
            if cmd in seen_execs:
                continue
            seen_execs.add(cmd)
            apps.append({"name": name, "exec": cmd})
        self.bus.emit(message.response({"apps": apps}))

    def handle_launch(self, message: Message) -> None:
        """Launch an application by spoken name.

        Request payload: ``{"name": "firefox"}``

        Response payload (success): ``{"name": "firefox", "success": true}``
        Response payload (error):   ``{"name": "firefox", "error": "..."}``
        """
        name: str = message.data.get("name", "")
        if not name:
            self.bus.emit(message.response({"name": name, "error": "Missing 'name' field"}))
            return

        cmd, score = match_one(name.title(), self.applist)
        if score < self._thresh:
            LOG.warning(f"[app-launcher] no app match for '{name}' (best score {score:.2f})")
            self.bus.emit(message.response({"name": name, "error": f"No application matched '{name}'"}))
            return

        LOG.info(f"[app-launcher] launching '{name}' → '{cmd}' (score {score:.2f})")
        try:
            subprocess.Popen(shlex.split(cmd), shell=self._cfg("shell", False))
            self.bus.emit(message.response({"name": name, "success": True}))
        except Exception as exc:
            LOG.exception(f"[app-launcher] failed to launch '{cmd}'")
            self.bus.emit(message.response({"name": name, "error": str(exc)}))

    def handle_close(self, message: Message) -> None:
        """Close a running application by spoken name.

        Request payload: ``{"name": "firefox"}``
        """
        name: str = message.data.get("name", "")
        if not name:
            self.bus.emit(message.response({"name": name, "error": "Missing 'name' field"}))
            return

        success = False
        if self._wmctrl and not self._cfg("disable_window_manager", False):
            success = self._close_by_window(name)
        if not success:
            success = self._close_by_process(name)

        if success:
            self.bus.emit(message.response({"name": name, "success": True}))
        else:
            self.bus.emit(message.response({"name": name, "error": f"No running process matched '{name}'"}))

    def handle_is_running(self, message: Message) -> None:
        """Check whether an application is currently running.

        Request payload: ``{"name": "firefox"}``
        Response payload: ``{"name": "firefox", "running": true}``
        """
        name: str = message.data.get("name", "")
        running = self._is_running(name) if name else False
        self.bus.emit(message.response({"name": name, "running": running}))

    # ------------------------------------------------------------------
    # Process helpers
    # ------------------------------------------------------------------

    def _match_process(self, app: str) -> Iterable[psutil.Process]:
        cmd, _ = match_one(app.title(), self.applist)
        cmd_base = cmd.split(" ")[0].split("/")[-1]
        processes = sorted(
            psutil.process_iter(["pid", "name", "create_time"]),
            key=lambda p: p.info["create_time"],
            reverse=True,
        )
        for proc in processes:
            if proc.status() == "zombie":
                continue
            if fuzzy_match(cmd_base, proc.info["name"]) > 0.9:
                yield proc

    def _close_by_process(self, app: str) -> bool:
        terminated = []
        for proc in self._match_process(app):
            try:
                LOG.info(f"[app-launcher] terminating {proc.info['name']} (PID {proc.info['pid']})")
                proc.terminate()
                terminated.append(proc.info["pid"])
                if not self._cfg("terminate_all", False):
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                LOG.warning(f"[app-launcher] could not terminate {proc}")
        return bool(terminated)

    def _is_running(self, app: str) -> bool:
        if self._wmctrl and self._match_window(app):
            return True
        for _ in self._match_process(app):
            return True
        return False

    # ------------------------------------------------------------------
    # Window helpers (wmctrl)
    # ------------------------------------------------------------------

    def _get_window_process_mapping(self) -> List[WindowMatch]:
        windows = []
        try:
            result = subprocess.run([self._wmctrl, "-lp"], capture_output=True, text=True)
            if result.returncode != 0:
                return []
            for line in result.stdout.splitlines():
                fields = line.split()
                if len(fields) < 4:
                    continue
                window_id, pid, window_title = fields[0], fields[2], " ".join(fields[4:])
                try:
                    proc = psutil.Process(int(pid))
                    windows.append((window_id, proc, proc.create_time(), window_title))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except Exception:
            LOG.exception("[app-launcher] error reading wmctrl output")
        return windows[::-1]

    def _match_window(self, app: str) -> List[WindowMatch]:
        candidates = []
        best = 0.0
        for win in self._get_window_process_mapping():
            score = max(fuzzy_match(win[1].name(), app), fuzzy_match(win[3], app))
            if score < self._thresh:
                continue
            if score > best:
                candidates = []
            if score >= best:
                candidates.append(win)
                best = score
        return candidates

    def _close_by_window(self, app: str) -> bool:
        candidates = self._match_window(app)
        if not candidates:
            return False
        for win in candidates:
            try:
                subprocess.run([self._wmctrl, "-ic", win[0]])
            except Exception:
                pass
            if not self._cfg("terminate_all", False):
                break
        return True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        self.bus.remove("ovos.phal.app_launcher.list", self.handle_list)
        self.bus.remove("ovos.phal.app_launcher.launch", self.handle_launch)
        self.bus.remove("ovos.phal.app_launcher.close", self.handle_close)
        self.bus.remove("ovos.phal.app_launcher.is_running", self.handle_is_running)
        super().shutdown()
