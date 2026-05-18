"""
API views для пассивной проверки живости.

Эндпоинты:
  POST /biometric/liveness/check/
      Принимает multipart/form-data (поле `image`) или JSON (поле `image_base64`).
      Возвращает JSON-результат проверки живости.

  GET  /biometric/liveness/health/
      Состояние сервиса - загруженные модели, устройство, порог.
"""

import base64
import logging

from rest_framework import status
from rest_framework.parsers import MultiPartParser, JSONParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.liveness.service import LivenessService

logger = logging.getLogger('apps.liveness')


class LivenessCheckView(APIView):
    """
    POST /biometric/liveness/check/

    Multipart:
        Content-Type: multipart/form-data
        Поле: image (файл)

    JSON base64:
        Content-Type: application/json
        Тело: { "image_base64": "<base64>" }

    Ответ 200:
    {
        "success": true,
        "is_real": true,
        "score": 0.87,
        "label": 1,
        "label_name": "real",
        "raw_scores": [0.05, 0.87, 0.08],
        "face_bbox": [x, y, w, h],
        "error": null
    }
    """

    parser_classes = [MultiPartParser, JSONParser]

    def post(self, request):
        image_input = None

        if 'image' in request.FILES:
            image_input = request.FILES['image']

        elif 'image_base64' in request.data:
            raw_b64 = request.data['image_base64']
            if ',' in raw_b64:
                raw_b64 = raw_b64.split(',', 1)[1]
            try:
                image_input = base64.b64decode(raw_b64)
            except Exception:
                return Response(
                    {'success': False, 'error': 'Invalid base64 data.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if image_input is None:
            return Response(
                {
                    'success': False,
                    'error': (
                        'No image provided. '
                        'Send multipart file as "image" or '
                        'base64 string as "image_base64".'
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            service = LivenessService.get_instance()
            result = service.check(image_input)
        except Exception as exc:
            logger.exception('Liveness check failed')
            return Response(
                {'success': False, 'error': f'Inference error: {exc}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        response_data = {'success': True, **result.to_dict()}

        if result.error:
            return Response(response_data, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        return Response(response_data, status=status.HTTP_200_OK)


class LivenessHealthView(APIView):
    """
    GET /biometric/liveness/health/

    Возвращает статус сервиса: загруженные модели, устройство, порог.
    """

    def get(self, request):
        try:
            service = LivenessService.get_instance()
            import os
            models_info = [
                {
                    'filename': os.path.basename(path),
                    'path': path,
                }
                for path in service.loaded_models
            ]
            return Response(
                {
                    'status': 'ok',
                    'device': str(service.device),
                    'models_loaded': len(models_info),
                    'models': models_info,
                    'real_threshold': service.real_threshold,
                }
            )
        except Exception as exc:
            logger.exception('Health check failed')
            return Response(
                {'status': 'error', 'detail': str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )