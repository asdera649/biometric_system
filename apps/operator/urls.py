from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='operator_dashboard'),
    path('logs/auth/', views.auth_logs, name='auth_logs'),
    path('logs/system/', views.system_logs, name='system_logs'),
    path('users/', views.users_list, name='users_list'),
    path('users/<int:user_id>/', views.user_detail, name='user_detail'),
    path('reports/', views.reports, name='reports'),
    path('reports/snapshot/', views.save_metrics_snapshot, name='save_metrics_snapshot'),
    path('settings/', views.system_settings, name='system_settings'),
    path('door/', views.door_logs, name='door_logs'),
]
