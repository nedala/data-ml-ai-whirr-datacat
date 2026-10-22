"""Sidebar navigation: Studio's own pages plus plugin PAGE dicts."""

from __future__ import annotations

from typing import Any

import auth
import pluginhost

GROUPS = ("Launch", "Configure", "Govern")

CORE_PAGES: list[dict[str, Any]] = [
    {"id": "story", "title": "Story", "icon": "rocket", "group": "Launch", "order": 5},
    {"id": "launchpad", "title": "Launchpad", "icon": "home", "group": "Launch", "order": 10},
    {"id": "compose", "title": "Start & stop", "icon": "wand", "group": "Launch", "order": 20},
    {"id": "credentials", "title": "Credentials", "icon": "key", "group": "Configure", "order": 60, "admin": True},
    {"id": "identities", "title": "Cloud identities", "icon": "cloud", "group": "Configure", "order": 65, "admin": True},
    {"id": "certificates", "title": "Certificates", "icon": "shield", "group": "Configure", "order": 70, "admin": True},
    {"id": "users", "title": "Users", "icon": "user-list", "group": "Govern", "order": 80, "admin": True},
    {"id": "backup", "title": "Backup & restore", "icon": "history", "group": "Govern", "order": 85, "admin": True},
    {"id": "services", "title": "Services", "icon": "cpu", "group": "Govern", "order": 90},
    {"id": "audit", "title": "Audit log", "icon": "document", "group": "Govern", "order": 95, "admin": True},
]
for _p in CORE_PAGES:
    _p["script"] = f"/studio/static/pages/{_p['id']}.js"


def nav(user: auth.User) -> list[dict[str, Any]]:
    is_admin = auth.has_role(user, "admin")
    pages = [p for p in CORE_PAGES + pluginhost.registry.pages() if is_admin or not p.get("admin")]
    rank = {g: i for i, g in enumerate(GROUPS)}
    return sorted(pages, key=lambda p: (rank.get(p.get("group", "Configure"), 9), p.get("order", 50)))


def plugin_errors(user: auth.User) -> list[dict[str, str]]:
    return list(pluginhost.registry.errors) if auth.has_role(user, "admin") else []
