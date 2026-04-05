import base64
import io

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib import messages
from django.core.files.base import ContentFile
from PIL import Image

from .models import CustomUser, BiometricTemplate


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        ('Биометрия', {'fields': ('patronymic', 'phone', 'is_operator', 'is_biometric_registered')}),
    )
    list_display = ['username', 'get_full_name_ru', 'is_operator', 'is_biometric_registered', 'is_active']


@admin.register(BiometricTemplate)
class BiometricTemplateAdmin(admin.ModelAdmin):
    list_display = ['user', 'detection_confidence', 'image_quality_score', 'created_at']
    readonly_fields = ['embedding', 'detection_confidence', 'image_quality_score', 'created_at', 'updated_at']

    def save_model(self, request, obj, form, change):
        new_image = request.FILES.get('face_image')

        if not new_image:
            super().save_model(request, obj, form, change)
            return

        try:
            from apps.biometric.face_processor import get_processor

            processor = get_processor()

            if not processor.is_ready:
                self.message_user(
                    request,
                    'Модель распознавания лиц недоступна. Вектор не создан.',
                    level=messages.ERROR,
                )
                super().save_model(request, obj, form, change)
                return

            # Конвертируем загруженный файл в base64
            new_image.seek(0)
            raw_bytes = new_image.read()
            image_data = 'data:image/jpeg;base64,' + base64.b64encode(raw_bytes).decode('utf-8')

            result = processor.process_registration_image(image_data)

            if not result['success']:
                self.message_user(
                    request,
                    f'Ошибка обработки лица: {result["error"]}',
                    level=messages.ERROR,
                )
                super().save_model(request, obj, form, change)
                return

            obj.embedding = result['embedding']
            obj.detection_confidence = round(result['detection_confidence'] * 100, 2)
            obj.image_quality_score = round(result['quality_score'] * 100, 2)

            try:
                img = Image.open(io.BytesIO(raw_bytes)).convert('RGB')
                img.thumbnail((200, 200))
                thumb_io = io.BytesIO()
                img.save(thumb_io, format='JPEG', quality=100)
                obj.face_image.save(
                    f'user_{obj.user.pk}.jpg',
                    ContentFile(thumb_io.getvalue()),
                    save=False,
                )
            except Exception:
                pass

            super().save_model(request, obj, form, change)

            obj.user.is_biometric_registered = True
            obj.user.save(update_fields=['is_biometric_registered'])

            self.message_user(
                request,
                f'Биометрия успешно зарегистрирована. '
                f'Уверенность обнаружения: {obj.detection_confidence:.1f}%, '
                f'качество: {obj.image_quality_score:.1f}%.',
                level=messages.SUCCESS,
            )

        except Exception as e:
            self.message_user(
                request,
                f'Ошибка обработки изображения: {e}',
                level=messages.ERROR,
            )
            super().save_model(request, obj, form, change)