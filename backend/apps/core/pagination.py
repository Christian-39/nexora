from rest_framework.pagination import CursorPagination
from rest_framework.response import Response
class StandardCursorPagination(CursorPagination):
    page_size=30;page_size_query_param='page_size';max_page_size=100;ordering='-created_at'
    def get_paginated_response(self,data):
        return Response({'success':True,'message':'Results retrieved','data':{'next':self.get_next_link(),'previous':self.get_previous_link(),'results':data}})
