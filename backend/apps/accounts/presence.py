import asyncio,time
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from channels.db import database_sync_to_async
from apps.core import cache as safe_cache
from django.utils import timezone
class PresenceConsumer(AsyncJsonWebsocketConsumer):
 async def connect(self):
  if not self.scope['user'].is_authenticated:return await self.close(code=4401)
  self.own=f'presence_user_{self.scope["user"].id}';self.watched=await self.allowed_groups()
  for group in self.watched:await self.channel_layer.group_add(group,self.channel_name)
  await self.accept();self.expiry_task=asyncio.create_task(self.expire());first=await self.increment()
  if first:await self.channel_layer.group_send(self.own,{'type':'presence.event','data':{'user':str(self.scope['user'].id),'online':True,'last_seen':None}})
  await self.send_json({'type':'presence.snapshot','data':await self.snapshot()})
 async def disconnect(self,code):
  if hasattr(self,'expiry_task'):self.expiry_task.cancel()
  if not hasattr(self,'watched'):return
  for group in self.watched:await self.channel_layer.group_discard(group,self.channel_name)
  last=await self.decrement()
  if last:
   stamp=timezone.now().isoformat();await self.set_last_seen();visible=stamp if self.scope['user'].show_last_seen else None;await self.channel_layer.group_send(self.own,{'type':'presence.event','data':{'user':str(self.scope['user'].id),'online':False,'last_seen':visible}})
 async def expire(self):
  delay=max(0,(self.scope.get('token_exp') or int(time.time()))-int(time.time()));await asyncio.sleep(delay);await self.close(code=4401)
 async def presence_event(self,event):await self.send_json({'type':'presence','data':event['data']})
 @database_sync_to_async
 def allowed_groups(self):
  from .models import User
  u=self.scope['user'];ids={u.id}
  if u.role=='ADMIN':ids.update(User.objects.filter(role='MEMBER',is_active=True).values_list('id',flat=True))
  else:
   ids.update(User.objects.filter(role='ADMIN',is_active=True).values_list('id',flat=True))
   ids.update(User.objects.filter(conversation_memberships__conversation__participants__user=u,conversation_memberships__conversation__kind='GROUP',conversation_memberships__is_active=True,is_active=True).values_list('id',flat=True))
  return [f'presence_user_{x}' for x in ids]
 @database_sync_to_async
 def snapshot(self):
  result=[]
  for group in self.watched:
   uid=group.removeprefix('presence_user_');result.append({'user':uid,'online':bool(safe_cache.safe_get(f'presence:{uid}',0,operation='presence'))})
  return result
 @database_sync_to_async
 def increment(self):
  key=f'presence:{self.scope["user"].id}';safe_cache.safe_add(key,0,120,operation='presence');value=safe_cache.safe_incr(key,operation='presence');
  if value is None:safe_cache.safe_set(key,1,120,operation='presence');value=1
  safe_cache.safe_touch(key,120,operation='presence');return value==1
 @database_sync_to_async
 def decrement(self):
  key=f'presence:{self.scope["user"].id}'
  value=safe_cache.safe_decr(key,operation='presence') or 0
  if value<=0:safe_cache.safe_delete(key,operation='presence');return True
  safe_cache.safe_touch(key,120,operation='presence');return False
 @database_sync_to_async
 def set_last_seen(self):
  from .models import User
  User.objects.filter(id=self.scope['user'].id).update(last_seen=timezone.now())
