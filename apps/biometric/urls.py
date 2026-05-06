"""
apps/biometric/urls.py
"""
from django.urls import path
from . import views

urlpatterns = [
    path('api/register/',          views.api_register_biometric, name='api_register_biometric'),
    path('api/authenticate/',      views.api_authenticate,       name='api_authenticate'),
    path('api/liveness/frame/',    views.api_liveness_frame,     name='api_liveness_frame'),
    path('api/liveness/cancel/',   views.api_liveness_cancel,    name='api_liveness_cancel'),
]
