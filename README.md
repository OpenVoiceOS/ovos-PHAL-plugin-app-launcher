# ovos-PHAL-plugin-app-launcher

PHAL plugin for [OpenVoiceOS](https://openvoiceos.com) that handles OS-level desktop
application management on behalf of
[ovos-skill-application-launcher](https://github.com/OpenVoiceOS/ovos-skill-application-launcher)
(or any other bus client).

---

## Why a split architecture?

In a standard single-box OVOS install the skill and the OS live on the same machine,
so calling `subprocess.Popen` directly from the skill works.  The split becomes
necessary when:

* **ovos-core runs on a server / NUC** while the desktop runs on a different device.
* **The device OS changes** (e.g. X11 desktop → Wayland → headless kiosk): swap only
  the PHAL plugin without touching the skill.
* **HiveMind multi-room setups**: the skill lives in ovos-core satellite; the PHAL
  plugin runs on the satellite device where apps should actually open.  Bus messages cross
  HiveMind transparently.

```
┌──────────────── ovos-core machine ─────────────────┐
│  ovos-skill-application-launcher                    │
│    • understands speech / intents                   │
│    • emits ovos.phal.app_launcher.* requests        │
│    • waits for responses, speaks result / error     │
└────────────────────┬───────────────────────────────┘
                     │  messagebus  (local or HiveMind)
┌────────────────── device machine ──────────────────┐
│  ovos-PHAL-plugin-app-launcher  (this repo)         │
│    • scans .desktop files                           │
│    • launches / closes processes via subprocess     │
│    • manages windows via wmctrl (optional)          │
│    • replies success / error                        │
└────────────────────────────────────────────────────┘
```

---

## Bus event API

All pairs use the standard `message.response()` convention so
`bus.wait_for_response` works across HiveMind sessions.

| Request event | Request payload | Response event | Response payload |
|---|---|---|---|
| `ovos.phal.app_launcher.list` | *(none)* | `.response` | `{apps: [{name, exec}]}` |
| `ovos.phal.app_launcher.launch` | `{name: str}` | `.response` | `{name, success: true}` or `{name, error: str}` |
| `ovos.phal.app_launcher.close` | `{name: str}` | `.response` | `{name, success: true}` or `{name, error: str}` |
| `ovos.phal.app_launcher.is_running` | `{name: str}` | `.response` | `{name, running: bool}` |

---

## Configuration

Add to `ovos.conf` under the `PHAL` section or plugin settings:

```json
{
  "ovos-phal-plugin-app-launcher": {
    "skip_categories": ["Settings", "ConsoleOnly", "Building"],
    "skip_keywords": [],
    "target_categories": [],
    "target_keywords": [],
    "blacklist": [],
    "aliases": {"kcalc": ["calculator"]},
    "user_commands": {},
    "require_icon": true,
    "require_categories": true,
    "match_threshold": 0.85,
    "terminate_all": false,
    "disable_window_manager": false
  }
}
```

| Key | Type | Default | Description |
|---|---|---|---|
| `skip_categories` | list[str] | `["Settings","ConsoleOnly","Building"]` | Desktop categories to exclude |
| `skip_keywords` | list[str] | `[]` | Desktop keywords to exclude |
| `target_categories` | list[str] | `[]` | Only include apps in these categories (empty = all) |
| `target_keywords` | list[str] | `[]` | Only include apps with these keywords (empty = all) |
| `blacklist` | list[str] | `[]` | App names or `.desktop` filenames to skip |
| `aliases` | dict | `{"kcalc":["calculator"]}` | Map desktop-name → speech aliases |
| `user_commands` | dict | `{}` | Map voice name → shell command |
| `require_icon` | bool | `true` | Skip apps without an `Icon` field |
| `require_categories` | bool | `true` | Skip apps without a `Categories` field |
| `match_threshold` | float | `0.85` | Fuzzy-match threshold (0–1) |
| `terminate_all` | bool | `false` | Kill every matching process, not just the first |
| `disable_window_manager` | bool | `false` | Skip `wmctrl`-based window management |

---

## Installation

```bash
pip install ovos-PHAL-plugin-app-launcher
# optional: for window management
sudo apt install wmctrl
```

---

## Credits

Developed by [TigreGótico](https://tigregotico.pt) for
[OpenVoiceOS](https://openvoiceos.org).

[![NGI0 Commons Fund](./ngi.png)](https://nlnet.nl/project/OpenVoiceOS)

This project was funded through the [NGI0 Commons Fund](https://nlnet.nl/commonsfund),
a fund established by [NLnet](https://nlnet.nl) with financial support from the
European Commission's [Next Generation Internet](https://ngi.eu) programme, under
the aegis of [DG Communications Networks, Content and Technology](https://commission.europa.eu/about-european-commission/departments-and-executive-agencies/communications-networks-content-and-technology_en)
under grant agreement No [101135429](https://cordis.europa.eu/project/id/101135429).
