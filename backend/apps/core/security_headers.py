class SecurityHeadersMiddleware:
    def __init__(self,get_response):self.get_response=get_response
    def __call__(self,request):
        response=self.get_response(request)
        response.setdefault('Content-Security-Policy',"default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        response.setdefault('Permissions-Policy','camera=(), microphone=(), geolocation=(), payment=(), usb=()')
        response.setdefault('Cross-Origin-Resource-Policy','same-site')
        response.setdefault('X-Permitted-Cross-Domain-Policies','none')
        return response
