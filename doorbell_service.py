"""
doorbell_service.py — Сервис домофона

Читает RTSP-поток с камеры домофона, находит лица на кадрах
и отправляет их в biometric system для идентификации

Требования:
    pip install opencv-python-headless requests facenet-pytorch torch

Запуск:
    python doorbell_service.py
"""

import os
import cv2
import time
import base64
import logging
import requests
import numpy as np
from datetime import datetime
from io import BytesIO
from PIL import Image

CONFIG = {
    # RTSP-поток домофона
    'RTSP_URL': os.getenv(
        'RTSP_URL',
        'rtsp://admin:microimpuls25@192.168.88.211:554/Streaming/Channels/101'
    ),

    # URL системы
    'API_BASE_URL': os.getenv('API_BASE_URL', 'http://127.0.0.1:8000'),

    # Токен домофона
    'SERVICE_TOKEN': os.getenv('SERVICE_TOKEN', 'токен-домофона'),

    # Сколько секунд ждать между попытками идентификации
    'IDENTIFY_INTERVAL_SEC': float(os.getenv('IDENTIFY_INTERVAL', '3.0')),

    # После успешного открытия, задержка перед следующей идентификацией
    'SUCCESS_COOLDOWN_SEC': float(os.getenv('SUCCESS_COOLDOWN', '10.0')),

    # Минимальный размер лица в пикселях (фильтрует далёкие/мелкие лица)
    'MIN_FACE_SIZE_PX': int(os.getenv('MIN_FACE_SIZE', '80')),

    # Качество JPEG при отправке в API в процентах
    'JPEG_QUALITY': int(os.getenv('JPEG_QUALITY', '85')),

    # Таймаут HTTP-запроса к API в секундах
    'API_TIMEOUT_SEC': int(os.getenv('API_TIMEOUT', '10')),

    # Логировать каждый кадр без лица?
    'LOG_NO_FACE': False,
}

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('doorbell')

class FaceDetector:
    """
    Лёгкий детектор на Haar Cascade для быстрой фильтрации кадров
    Если лицо найдено, вырезаем и отправляем в API

    """

    def __init__(self):
        cascade_path = cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
        self.cascade = cv2.CascadeClassifier(cascade_path)
        log.info(f'Инициализирован детектор лиц: {cascade_path}')

    def detect(self, frame_bgr: np.ndarray):
        """
        Возвращает список (x, y, w, h) найденных лиц,
        отфильтровывает слишком маленькие лица
        """
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        faces = self.cascade.detectMultiScale(
            gray,
            scaleFactor=1.1,
            minNeighbors=5,
            minSize=(CONFIG['MIN_FACE_SIZE_PX'], CONFIG['MIN_FACE_SIZE_PX'])
        )
        if len(faces) == 0:
            return []
        return [tuple(f) for f in faces]

    def crop_largest(self, frame_bgr: np.ndarray, faces: list):
        """
        Вырезает наибольшее лицо из кадра с небольшим отступом
        """
        if not faces:
            return None
        # Берём наибольшее лицо (по площади)
        x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
        padding = int(max(w, h) * 0.25)
        H, W = frame_bgr.shape[:2]
        x1 = max(0, x - padding)
        y1 = max(0, y - padding)
        x2 = min(W, x + w + padding)
        y2 = min(H, y + h + padding)
        return frame_bgr[y1:y2, x1:x2]


# API часть

def encode_frame_to_base64(frame_bgr: np.ndarray) -> str:
    """Конвертирует BGR кадр OpenCV в base64 JPEG для отправки в API"""

    frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    img = Image.fromarray(frame_rgb)

    buf = BytesIO()
    img.save(buf, format='JPEG', quality=CONFIG['JPEG_QUALITY'])
    b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
    return f'data:image/jpeg;base64,{b64}'


def call_identify_api(image_data: str) -> dict:
    """
    Отправляет кадр в /biometric/api/identify/ и возвращает ответ
    """
    url = CONFIG['API_BASE_URL'].rstrip('/') + '/biometric/api/identify/'
    try:
        resp = requests.post(
            url,
            json={
                'image_data': image_data,
                'service_token': CONFIG['SERVICE_TOKEN'],
                'open_door': True,
            },
            timeout=CONFIG['API_TIMEOUT_SEC']
        )
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.ConnectionError:
        log.error(f'Не удалось подключиться к {url}')
        return {'identified': False, 'error': 'connection_error'}
    except requests.exceptions.Timeout:
        log.error(f'Таймаут запроса к {url}')
        return {'identified': False, 'error': 'timeout'}
    except Exception as e:
        log.error(f'Ошибка API запроса: {e}')
        return {'identified': False, 'error': str(e)}


# Основной цикл

def run_doorbell_service():
    detector = FaceDetector()

    last_identify_time = 0.0
    last_success_time  = 0.0
    reconnect_delay    = 5  # секунд между попытками переподключения

    log.info(f'Подключение к RTSP: {CONFIG["RTSP_URL"]}')
    log.info(f'API URL: {CONFIG["API_BASE_URL"]}')

    while True:
        cap = cv2.VideoCapture(CONFIG['RTSP_URL'])
        if not cap.isOpened():
            log.error(f'Не удалось открыть RTSP поток. Повтор через {reconnect_delay}s...')
            time.sleep(reconnect_delay)
            continue

        log.info('RTSP поток успешно открыт')
        consecutive_errors = 0

        while True:
            ret, frame = cap.read()
            if not ret:
                consecutive_errors += 1
                if consecutive_errors > 10:
                    log.warning('Много ошибок чтения кадров. Переподключение...')
                    break
                time.sleep(0.1)
                continue

            consecutive_errors = 0
            now = time.time()

            # Задержка после успешного открытия
            if now - last_success_time < CONFIG['SUCCESS_COOLDOWN_SEC']:
                continue

            # Интервал между идентификациями
            if now - last_identify_time < CONFIG['IDENTIFY_INTERVAL_SEC']:
                continue

            # Обнаружение лица
            faces = detector.detect(frame)
            if not faces:
                if CONFIG['LOG_NO_FACE']:
                    log.debug('Нет лиц в кадре')
                continue

            log.info(f'Обнаружено {len(faces)} лиц в кадре')

            # Вырезаем наибольшее лицо
            face_crop = detector.crop_largest(frame, faces)
            if face_crop is None:
                continue

            # Кодируем и отправляем в API
            image_data = encode_frame_to_base64(face_crop)
            last_identify_time = now

            log.info('Отправка в API...')
            result = call_identify_api(image_data)

            if result.get('identified'):
                user     = result.get('full_name') or result.get('username', '?')
                conf     = result.get('recognition_confidence', 0) * 100
                opened   = result.get('door_opened', False)
                last_success_time = now
                log.info(
                    f'Успешно: {user} (conf={conf:.1f}%, door_opened={opened})'
                )
            else:
                conf  = result.get('recognition_confidence', 0) * 100
                error = result.get('error', 'unknown')
                log.info(f'Доступ отклонен: {error} (best_conf={conf:.1f}%)')

        cap.release()
        log.info(f'Переподключение через {reconnect_delay}с...')
        time.sleep(reconnect_delay)


# Точка входа

if __name__ == '__main__':
    try:
        run_doorbell_service()
    except KeyboardInterrupt:
        log.info('Сервис остановлен')