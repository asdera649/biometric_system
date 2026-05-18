from django.urls import path
from apps.liveness.views import LivenessCheckView, LivenessHealthView

app_name = 'liveness'

urlpatterns = [
    path('check/', LivenessCheckView.as_view(), name='check'),
    path('health/', LivenessHealthView.as_view(), name='health'),
]