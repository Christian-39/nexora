from rest_framework.views import exception_handler
def api_exception_handler(exc,context):
 r=exception_handler(exc,context)
 if r is None:return r
 details=r.data; code=getattr(exc,'default_code','ERROR')
 message='Request failed.'
 if isinstance(details,dict) and 'detail' in details: message=str(details['detail']); details={}
 r.data={'success':False,'message':message,'code':str(code).upper(),'errors':details}
 return r
