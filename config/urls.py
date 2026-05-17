from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.views.generic import RedirectView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('apps.accounts.urls')),
    path('biometric/', include('apps.biometric.urls')),
    path('operator/', include('apps.operator.urls')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
