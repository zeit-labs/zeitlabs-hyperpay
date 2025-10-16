"""
URLs for hyperpay.
"""
from django.urls import re_path

from . import views

app_name = 'hyperpay'

urlpatterns = [
    re_path(r'^return/$', views.HyperPayReturnView.as_view(), name='return'),
    re_path(r'^status/$', views.HyperPayStatusView.as_view(), name='status'),
    re_path(r'^callback/$', views.HyperPayWebhookView.as_view(), name='webhook'),
]
