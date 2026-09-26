"""
NEXORA — uniform response envelope.

Every JSON response the API produces has exactly one shape:

    success: {"success": true,  "message": "...", "data": ...}
    failure: {"success": false, "message": "...", "code": "...", "errors": {}}

Hand-written views build the envelope themselves; generic DRF machinery
(ModelViewSet list/retrieve/create/update) does not, so this renderer wraps
whatever it is given. Payloads that are already enveloped pass through
untouched, and streamed/file responses never reach a renderer at all.
"""

from rest_framework.renderers import JSONRenderer

DEFAULT_MESSAGES = {
    "GET": "Request successful",
    "POST": "Created",
    "PUT": "Updated",
    "PATCH": "Updated",
    "DELETE": "Deleted",
}


def is_enveloped(data) -> bool:
    return isinstance(data, dict) and "success" in data and ("data" in data or "code" in data)


class EnvelopeJSONRenderer(JSONRenderer):
    def render(self, data, accepted_media_type=None, renderer_context=None):
        context = renderer_context or {}
        response = context.get("response")
        request = context.get("request")

        if response is not None and not is_enveloped(data):
            status = response.status_code
            if status == 204 or data is None:
                data = {
                    "success": True,
                    "message": DEFAULT_MESSAGES.get(getattr(request, "method", "GET"), "Request successful"),
                    "data": {},
                }
                response.status_code = 200 if status == 204 else status
            elif 200 <= status < 300:
                data = {
                    "success": True,
                    "message": DEFAULT_MESSAGES.get(getattr(request, "method", "GET"), "Request successful"),
                    "data": data,
                }
            else:
                # The exception handler normally produces this shape; this is
                # the fallback for errors raised outside it.
                detail = data.get("detail") if isinstance(data, dict) else None
                data = {
                    "success": False,
                    "message": str(detail) if detail else "Request failed.",
                    "code": f"HTTP_{status}",
                    "errors": data if isinstance(data, dict) and not detail else {},
                }

        return super().render(data, accepted_media_type, renderer_context)
