"""Security response headers.

Every value comes from ``django.conf.settings`` so a deployment can review and
change its posture in exactly one place. The previous implementation
hard-coded ``Cross-Origin-Resource-Policy: same-site``, which silently broke
legitimate cross-site embedding (member avatars, branding images) on the
documented Vercel(frontend) → Render(API) split deployment, where the two
origins are different *sites*: the browser refused to embed any API-served
image into the frontend page.
"""

from django.conf import settings


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.setdefault(
            "Content-Security-Policy",
            getattr(settings, "CONTENT_SECURITY_POLICY", "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
        )
        response.setdefault(
            "Permissions-Policy",
            getattr(
                settings,
                "PERMISSIONS_POLICY",
                "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
            ),
        )
        # ``cross-origin`` is the value this deployment requires: the API and
        # the static frontend live on different sites, and CORP governs
        # *embedding*, not reading — authentication still protects every byte.
        response.setdefault(
            "Cross-Origin-Resource-Policy",
            getattr(settings, "CROSS_ORIGIN_RESOURCE_POLICY", "cross-origin"),
        )
        response.setdefault("X-Permitted-Cross-Domain-Policies", "none")
        return response
