import asyncio,time
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from channels.db import database_sync_to_async
from django.utils import timezone
class ConversationConsumer(AsyncJsonWebsocketConsumer):
 async def connect(self):
  self.cid=self.scope['url_route']['kwargs']['conversation_id'];self.group=f'conversation_{self.cid}'
  if not self.scope['user'].is_authenticated or not await self.allowed():return await self.close(code=4403)
  await self.mark_delivered();await self.channel_layer.group_add(self.group,self.channel_name);await self.accept();self.expiry_task=asyncio.create_task(self.expire())
 async def disconnect(self,code):
  if hasattr(self,'expiry_task'):self.expiry_task.cancel()
  if hasattr(self,'typing_task'):self.typing_task.cancel()
  if hasattr(self,'group'):await self.channel_layer.group_discard(self.group,self.channel_name)
 @database_sync_to_async
 def allowed(self):return self.scope['user'].conversation_memberships.filter(conversation_id=self.cid,is_active=True,conversation__is_active=True).exists()
 @database_sync_to_async
 def mark_delivered(self):
  from .models import MessageReceipt
  return MessageReceipt.objects.filter(message__conversation_id=self.cid,recipient=self.scope['user'],delivered_at__isnull=True).update(delivered_at=timezone.now())
 async def receive_json(self,data):
  if data.get('type')=='typing':
   typing=bool(data.get('typing'));await self.channel_layer.group_send(self.group,{'type':'typing.event','user':str(self.scope['user'].id),'typing':typing})
   if hasattr(self,'typing_task'):self.typing_task.cancel()
   if typing:self.typing_task=asyncio.create_task(self.clear_typing())
 async def expire(self):
  delay=max(0,(self.scope.get('token_exp') or int(time.time()))-int(time.time()))
  await asyncio.sleep(delay);await self.close(code=4401)
 async def clear_typing(self):
  await asyncio.sleep(8);await self.channel_layer.group_send(self.group,{'type':'typing.event','user':str(self.scope['user'].id),'typing':False})
 async def message_new(self,event):await self.send_json({'type':'message.new','data':event['data']})
 async def typing_event(self,event):await self.send_json({'type':'typing','user':event['user'],'typing':event['typing']})
 async def receipt_event(self,event):await self.send_json({'type':'receipt','data':event['data']})
 async def message_changed(self,event):await self.send_json({'type':'message.changed','data':event['data']})
