import time
import os,tempfile
from django.core.files import File
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand
from django.db import transaction
from apps.media.models import UploadSession
from apps.media.validators import validate_upload
from apps.conversations.models import Attachment
from apps.conversations.services import send_message
class Command(BaseCommand):
 help='Validate completed direct uploads and atomically publish media messages.'
 def add_arguments(self,p):p.add_argument('--once',action='store_true')
 def handle(self,*args,**opts):
  while True:
   with transaction.atomic():
    s=UploadSession.objects.select_for_update(skip_locked=True).filter(state='UPLOADED').select_related('user','conversation').first()
    if not s:
     if opts['once']:break
     time.sleep(2);continue
    s.state='PROCESSING';s.save(update_fields=['state','updated_at'])
   self.finalize(s)
   if opts['once']:break
 def finalize(self,s):
  path=None
  try:
   suffix=os.path.splitext(s.original_name)[1];tmp=tempfile.NamedTemporaryFile(suffix=suffix,delete=False);path=tmp.name
   with default_storage.open(s.storage_key,'rb') as src:
    for chunk in iter(lambda:src.read(1024*1024),b''):tmp.write(chunk)
   tmp.close()
   with open(path,'rb') as raw:
    upload=File(raw,name=s.original_name);upload.size=s.expected_size;mime,(w,h),duration=validate_upload(upload,s.media_type)
   with transaction.atomic():
    message,created=send_message(user=s.user,conversation=s.conversation,client_id=s.client_id,text='',type=s.media_type)
    if created:Attachment.objects.create(message=message,storage_key=s.storage_key,original_name=s.original_name,mime_type=mime,size=s.expected_size,width=w,height=h,duration_ms=duration)
    s.message=message;s.state='COMPLETE';s.error_code='';s.save(update_fields=['message','state','error_code','updated_at'])
  except Exception as exc:
   default_storage.delete(s.storage_key);s.state='FAILED';s.error_code=exc.__class__.__name__[:60];s.save(update_fields=['state','error_code','updated_at'])
  finally:
   if path:
    try:os.unlink(path)
    except OSError:pass
