from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import CustomUser, BiometricTemplate

@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (('Биометрия', {'fields': ('patronymic', 'phone', 'is_operator', 'is_biometric_registered')}),)
    list_display = ['username', 'get_full_name_ru', 'is_operator', 'is_biometric_registered', 'is_active']

@admin.register(BiometricTemplate)
class BiometricTemplateAdmin(admin.ModelAdmin):
    list_display = ['user', 'detection_confidence', 'image_quality_score', 'created_at']
    readonly_fields = ['created_at', 'updated_at']
