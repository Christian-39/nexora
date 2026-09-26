from rest_framework import serializers
from .models import *
class ConversationSerializer(serializers.ModelSerializer):
 class Meta:model=Conversation;fields=['id','kind','admin','member','is_active','updated_at'];read_only_fields=fields
class MessageCreateSerializer(serializers.Serializer):
 client_id=serializers.UUIDField();type=serializers.ChoiceField(choices=['TEXT'],default='TEXT');text=serializers.CharField();reply_to=serializers.PrimaryKeyRelatedField(queryset=Message.objects.all(),required=False,allow_null=True)
class AttachmentSerializer(serializers.ModelSerializer):
 original_url=serializers.SerializerMethodField();thumbnail_url=serializers.SerializerMethodField();optimized_url=serializers.SerializerMethodField()
 class Meta:model=Attachment;fields=['id','mime_type','size','duration_ms','width','height','processing_state','original_url','thumbnail_url','optimized_url'];read_only_fields=fields
 def _url(self,obj,variant='original'):
  request=self.context.get('request');path=f'/api/media/{obj.id}/';path+=f'?variant={variant}' if variant!='original' else ''
  return request.build_absolute_uri(path) if request else path
 def get_original_url(self,obj):return self._url(obj)
 def get_thumbnail_url(self,obj):return self._url(obj,'thumbnail') if obj.thumbnail_key else None
 def get_optimized_url(self,obj):return self._url(obj,'optimized') if obj.optimized_key else None
class MessageSerializer(serializers.ModelSerializer):
 attachment=AttachmentSerializer(read_only=True);delivery=serializers.SerializerMethodField()
 class Meta:model=Message;fields=['id','conversation','sender','client_id','type','text','reply_to','attachment','delivery','created_at','updated_at','edited_at','deleted_at'];read_only_fields=['id','sender','created_at','updated_at','edited_at','deleted_at']
 def get_delivery(self,obj):
  receipts=list(obj.receipts.all());total=len(receipts);read=sum(bool(x.read_at) for x in receipts);delivered=sum(bool(x.delivered_at) for x in receipts)
  state='READ' if total and read==total else 'DELIVERED' if total and delivered==total else 'SENT'
  return {'state':state,'recipients':total,'delivered':delivered,'read':read}
class ReactionSerializer(serializers.ModelSerializer):
 class Meta:model=MessageReaction;fields=['id','message','user','reaction','created_at'];read_only_fields=['id','user','created_at']
