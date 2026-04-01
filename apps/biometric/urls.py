from django.urls import path
from . import views

urlpatterns = [
    path('api/register/', views.api_register_biometric, name='api_register_biometric'),
    path('api/authenticate/', views.api_authenticate, name='api_authenticate'),
    path('api/identify/', views.api_identify, name='api_identify'),
]
