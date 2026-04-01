from django.urls import path
from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('accounts/register/', views.register_step1, name='register_step1'),
    path('accounts/register/biometric/', views.register_step2, name='register_step2'),
    path('accounts/login/', views.face_login, name='face_login'),
    path('accounts/capture/', views.face_capture, name='face_capture'),
    path('accounts/success/', views.auth_success, name='auth_success'),
    path('accounts/profile/', views.profile, name='profile'),
    path('accounts/logout/', views.logout_view, name='logout'),
]
