"""
Frontend ↔ backend contract tests.

These parse the real frontend source and assert it against the real Django URL
map and the real WebSocket routing. They are the guard against the exact class
of defect this codebase shipped with: a frontend calling endpoints that were
never implemented, and a client listening for events the server never emits.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.urls import get_resolver
from django.urls.resolvers import URLPattern, URLResolver

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
JS = FRONTEND / "assets" / "js"

#: Paths the frontend builds from a backend-provided absolute URL, or which are
#: served by the static host rather than the API.
IGNORED_PREFIXES = ("/api/public/branding/",)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def backend_url_patterns() -> list[str]:
    """Every registered route as a regex-ish template string."""
    collected: list[str] = []

    def walk(patterns, prefix=""):
        for entry in patterns:
            if isinstance(entry, URLResolver):
                walk(entry.url_patterns, prefix + str(entry.pattern))
            elif isinstance(entry, URLPattern):
                collected.append(prefix + str(entry.pattern))

    walk(get_resolver().url_patterns)
    return collected


def to_regex(template: str) -> re.Pattern:
    """Turn a Django route template into a matcher for concrete paths.

    Handles both ``path()`` converters (``<uuid:pk>``) and ``re_path()`` named
    groups (``(?P<member_id>[0-9a-f-]+)``).
    """
    # Strip the anchors first: the segment placeholder contains '^' itself.
    pattern = template.replace("^", "").replace("$", "")
    pattern = re.sub(r"\(\?P<[^>]+>.*?\)", "\0SEG\0", pattern)
    pattern = re.sub(r"<[^:>]+:[^>]+>", "\0SEG\0", pattern)
    pattern = re.sub(r"<[^>]+>", "\0SEG\0", pattern)
    pattern = pattern.replace("\0SEG\0", "[^/]+")
    try:
        return re.compile(f"^/?{pattern}$")
    except re.error:  # pragma: no cover - a route we cannot model is skipped
        return re.compile(r"^\0$")


@pytest.fixture
def backend_matchers():
    return [to_regex(template) for template in backend_url_patterns()]


def frontend_api_paths() -> set[str]:
    """Every '/api/...' literal used in api.js, with placeholders normalised."""
    source = (JS / "api.js").read_text()
    found = set()
    # Collapse template-literal interpolations first so `${a ? 'x' : 'y'}`
    # cannot terminate the literal at its inner quote.
    normalised = re.sub(r"\$\{[^{}]*\}", "PLACEHOLDER", source)
    for raw in re.findall(r"['\"`](/api/[^'\"`]*)['\"`]", normalised):
        path = raw.split("?")[0]
        if not path.endswith("/"):
            path = f"{path}/"
        found.add(path)
    return found


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_frontend_config_module_exists():
    assert (JS / "config.js").is_file(), "the centralized runtime config module is missing"


#: Ternaries in api.js collapse to a placeholder; these are the real values.
CONDITIONAL_SEGMENTS = ("archive", "unarchive", "activate", "deactivate", "logo", "favicon")

UUID_PROBE = "00000000-0000-4000-8000-000000000000"


def probes_for(path: str) -> list[str]:
    """Concrete URLs a templated api.js path can produce."""
    candidates = [path.replace("PLACEHOLDER", UUID_PROBE)]
    if path.count("PLACEHOLDER") > 1:
        # The last placeholder is an action word, not an identifier.
        head, _, tail = path.rstrip("/").rpartition("PLACEHOLDER")
        for word in CONDITIONAL_SEGMENTS:
            candidates.append(f"{head}{word}{tail}/".replace("PLACEHOLDER", UUID_PROBE))
    return candidates


@pytest.mark.django_db
def test_every_endpoint_api_js_calls_is_routed(backend_matchers):
    missing = []
    for path in sorted(frontend_api_paths()):
        if path.startswith(IGNORED_PREFIXES):
            continue
        if not any(
            matcher.match(probe) for probe in probes_for(path) for matcher in backend_matchers
        ):
            missing.append(path)
    assert not missing, f"api.js calls endpoints the backend does not route: {missing}"


def test_no_module_other_than_api_performs_raw_backend_http():
    """api.js is the only HTTP layer for the backend.

    theme.js may read the *static* manifest template (a same-origin public
    asset, fetched without credentials); anything touching /api/ must not.
    """
    offenders = []
    for path in sorted(JS.glob("*.js")):
        if path.name in {"api.js", "config.js"}:
            continue
        source = path.read_text()
        if "XMLHttpRequest" in source:
            offenders.append(f"{path.name}: XMLHttpRequest")
        for match in re.finditer(r"(?<![.\w])fetch\s*\(([^)]{0,120})", source):
            argument = match.group(1)
            if "/api" in argument or "buildUrl" in argument or "credentials: 'include'" in argument:
                offenders.append(f"{path.name}: {argument.strip()[:60]}")
    assert not offenders, f"backend HTTP must go through api.js; found {offenders}"


def test_no_hardcoded_hosts_or_ports_in_frontend_modules():
    offenders = []
    for path in sorted(JS.glob("*.js")):
        source = path.read_text()
        for match in re.finditer(r"https?://(?!www\.w3\.org)[A-Za-z0-9.\-]+(:\d+)?", source):
            # config.js legitimately documents the local-dev convention.
            if path.name == "config.js":
                continue
            offenders.append(f"{path.name}: {match.group(0)}")
    assert not offenders, f"hardcoded origins found: {offenders}"


def test_websocket_path_used_by_the_frontend_is_routed():
    from apps.conversations.routing import websocket_urlpatterns

    sources = "\n".join(p.read_text() for p in JS.glob("*.js"))
    used = set(re.findall(r"['\"`](/ws/[^'\"`$]*)['\"`]", sources))
    assert used, "the frontend no longer opens any websocket"

    routes = [re.compile(str(entry.pattern)) for entry in websocket_urlpatterns]
    for path in used:
        probe = path.lstrip("/")
        assert any(route.match(probe) for route in routes), f"{path} is not routed by Channels"


def test_every_socket_event_the_frontend_listens_for_is_emitted_somewhere():
    """Each ``socketEvents.on('x')`` must correspond to a server-side emit."""
    sources = "\n".join(p.read_text() for p in JS.glob("*.js"))
    listened = set(re.findall(r"socketEvents\.on\(\s*'([a-z.]+)'", sources))

    backend_root = Path(__file__).resolve().parents[1] / "apps"
    backend_source = "\n".join(p.read_text() for p in backend_root.rglob("*.py"))

    # Transport-level events produced by the client itself, not the server.
    # Events produced by the client transport itself, never by the server.
    client_side = {"state", "unauthorized"}
    missing = sorted(
        name
        for name in listened - client_side
        if f'"{name}"' not in backend_source and f"'{name}'" not in backend_source
    )
    assert not missing, f"the frontend listens for events the backend never emits: {missing}"


def test_service_worker_precaches_the_config_module_and_never_caches_the_api():
    source = (FRONTEND / "sw.js").read_text()
    assert "assets/js/config.js" in source, "config.js must be part of the precached shell"
    assert re.search(r"PRIVATE_PATH\s*=\s*/\\?/api", source), "the /api/ network-only rule is missing"
    # Private responses must never be written to Cache Storage.
    assert "cache.put" in source
    for block in re.findall(r"if \(PRIVATE_PATH.test[^}]*}", source):
        assert "cache.put" not in block


def test_html_pages_declare_the_runtime_config_meta_tags():
    for page in FRONTEND.glob("*.html"):
        html = page.read_text()
        if "assets/js/" not in html:
            continue
        assert 'name="nexora-api-base"' in html, f"{page.name} is missing the API base meta tag"
        assert 'name="nexora-api-prefix"' in html, f"{page.name} is missing the API prefix meta tag"


def test_no_sensitive_values_are_written_to_web_storage():
    offenders = []
    for path in sorted(JS.glob("*.js")):
        for match in re.finditer(r"(localStorage|sessionStorage)\.setItem\(\s*([^,]+),", path.read_text()):
            key = match.group(2).lower()
            if any(word in key for word in ("token", "pin", "password", "secret", "auth", "access")):
                offenders.append(f"{path.name}: {match.group(0)}")
    assert not offenders, f"sensitive values must never be persisted: {offenders}"


def test_no_unsafe_innerhtml_or_eval_in_frontend():
    offenders = []
    for path in sorted(JS.glob("*.js")):
        source = path.read_text()
        for pattern in (r"\.innerHTML\s*=", r"(?<![.\w])eval\s*\(", r"new\s+Function\s*\(", r"insertAdjacentHTML"):
            for match in re.finditer(pattern, source):
                snippet = source[max(0, match.start() - 60) : match.end()]
                # Clearing a node with a constant empty string is safe.
                if re.search(r"\.innerHTML\s*=\s*''\s*;?$", snippet.strip()):
                    continue
                offenders.append(f"{path.name}: {match.group(0)}")
    assert not offenders, f"unsafe DOM/eval sinks found: {offenders}"


@pytest.mark.django_db
def test_every_json_response_uses_the_same_envelope(admin, member_a, private_thread):
    """Success and failure bodies must always have the documented shape."""
    from tests.conftest import authed, client_id

    client = authed(admin)
    checks = [
        client.get("/api/me/"),
        client.get("/api/members/"),
        client.get("/api/groups/"),
        client.get("/api/conversations/"),
        client.get(f"/api/conversations/{private_thread.id}/"),
        client.get("/api/settings/"),
        client.get("/api/unread/"),
        client.post(
            f"/api/conversations/{private_thread.id}/messages/",
            {"client_id": client_id(), "text": "envelope"},
            format="json",
        ),
    ]
    for response in checks:
        assert response.status_code < 400, response.request["PATH_INFO"]
        body = response.json()
        assert body["success"] is True, response.request["PATH_INFO"]
        assert "message" in body and "data" in body, response.request["PATH_INFO"]

    failures = [
        client.get("/api/conversations/00000000-0000-4000-8000-000000000000/"),
        client.post(f"/api/conversations/{private_thread.id}/messages/", {}, format="json"),
        authed(member_a).get("/api/settings/"),
    ]
    for response in failures:
        body = response.json()
        assert body["success"] is False
        assert "message" in body and "code" in body and "errors" in body


# ---------------------------------------------------------------------------
# Runtime origin resolution (Vercel frontend ↔ Render backend)
# ---------------------------------------------------------------------------

CSS = FRONTEND / "assets" / "css"

PRODUCTION_API = "https://nexora-backend-ptsc.onrender.com"
PRODUCTION_FRONTEND = "https://nexora-eight-lilac.vercel.app"


def test_config_is_the_only_module_that_names_the_backend_origin():
    """config.js resolves the API origin; nothing else may hardcode a host."""
    source = (JS / "config.js").read_text()
    assert PRODUCTION_API in source, "the deployed backend origin must be resolvable"
    assert PRODUCTION_FRONTEND not in source, "the static host must never be used as an API origin"

    for path in sorted(JS.glob("*.js")):
        if path.name == "config.js":
            continue
        assert "onrender.com" not in path.read_text(), f"{path.name} hardcodes the backend host"


def test_local_development_keeps_the_hostname_and_uses_port_8000():
    source = (JS / "config.js").read_text()
    assert "LOCAL_API_PORT" in source and "'8000'" in source
    # 127.0.0.1 must not be silently rewritten to localhost (cookies are
    # scoped by host), so the resolver reuses location.hostname.
    assert "location.hostname" in source


def test_the_websocket_origin_is_derived_from_the_api_origin():
    ws = (JS / "websocket.js").read_text()
    assert "apiConfig.wsOrigin" in ws
    # No second source of truth, and no scheme guessing from the page origin.
    assert "window.location.origin" not in ws
    config_source = (JS / "config.js").read_text()
    assert "toWebSocketOrigin" in config_source


# ---------------------------------------------------------------------------
# Realtime client: bounded, non-looping reconnection
# ---------------------------------------------------------------------------


def test_the_realtime_client_stops_instead_of_reconnecting_forever():
    source = (JS / "websocket.js").read_text()
    assert "AUTH_CLOSE_CODES" in source
    assert "MAX_RECONNECT_ATTEMPTS" in source
    assert "refreshSession" in source, "auth recovery must reuse the central HTTP refresh"
    assert "backoffDelay" in source, "reconnection must use bounded backoff with jitter"
    for state in ("idle", "connecting", "open", "reconnecting", "offline", "closed"):
        assert f"'{state}'" in source


def test_logout_stops_the_socket():
    source = (JS / "auth.js").read_text()
    assert "realtime.stop" in source


# ---------------------------------------------------------------------------
# Navigation: mobile hamburger + drawer, top-right profile/theme
# ---------------------------------------------------------------------------


def test_mobile_navigation_provides_an_accessible_hamburger_and_drawer():
    source = (JS / "navigation.js").read_text()
    assert "app-header__menu" in source, "the mobile hamburger button is missing"
    assert "'aria-controls': 'app-drawer'" in source
    assert "'aria-expanded'" in source
    assert "trapFocus" in source, "the drawer must trap focus while open"
    assert "'Escape'" in source, "the drawer must close on Escape"
    assert "data-drawer-backdrop" in source, "the drawer must have a backdrop"
    assert "export function closeDrawer" in source


def test_profile_and_theme_live_in_the_header_not_the_nav_footer():
    source = (JS / "navigation.js").read_text()
    header = source.split("function renderHeader", 1)[1]
    assert "app-header__controls" in header
    assert "Change appearance" in header
    assert "profile-menu-button" in header

    footer_block = source.split("const footer = el(", 1)[1].split("navRoot.append(footer)", 1)[0]
    assert "Change appearance" not in footer_block
    assert "profile.html" not in footer_block


def test_the_drawer_is_role_aware():
    source = (JS / "navigation.js").read_text()
    assert "DRAWER_ITEMS" in source
    assert "item.adminOnly && !admin" in source


def test_theme_control_delegates_to_the_theme_module():
    source = (JS / "navigation.js").read_text()
    assert "setTheme(pref)" in source
    assert "from './theme.js'" in source


# ---------------------------------------------------------------------------
# Notifications: top placement, one system, no layout hacks
# ---------------------------------------------------------------------------


def test_toasts_are_anchored_to_the_top_of_the_viewport():
    components = (CSS / "components.css").read_text()
    region = components.split(".toast-region {", 1)[1].split("}", 1)[0]
    assert "top:" in region
    assert "bottom:" not in region
    assert "safe-top" in region
    assert "app-header-h" in region, "toasts must clear the header controls"

    responsive = (CSS / "responsive.css").read_text()
    mobile_region = responsive.split(".toast-region {", 1)[1].split("}", 1)[0]
    assert "top:" in mobile_region and "bottom:" not in mobile_region


def test_there_is_exactly_one_notification_toast_system():
    creators = [
        path.name
        for path in JS.glob("*.js")
        if "class: 'toast-region'" in path.read_text() and path.name != "ui.js"
    ]
    assert not creators, f"a second toast system was introduced in {creators}"


def test_no_global_overflow_x_hidden_workaround():
    offenders = []
    for path in sorted(CSS.glob("*.css")):
        source = path.read_text()
        for match in re.finditer(r"([^{}]*)\{[^}]*overflow-x:\s*hidden", source):
            selector = match.group(1).strip().splitlines()[-1].strip()
            if selector in {"html", "body", "*", ":root", "html, body", "body.app-body"}:
                offenders.append(f"{path.name}: {selector}")
    assert not offenders, f"global overflow-x:hidden workaround found: {offenders}"


def test_the_mobile_breakpoint_does_not_inherit_the_desktop_sidebar():
    responsive = (CSS / "responsive.css").read_text()
    mobile_block = responsive.split("@media (max-width: 767px) {", 1)[1]
    assert ".app-nav { display: none; }" in mobile_block
    assert ".app-header__menu { display: inline-flex; }" in mobile_block
