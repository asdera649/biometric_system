"""
doorbell_service.py — Сервис домофона

Читает видеопоток (RTSP или локальный файл), находит лица на кадрах и отправляет их в biometric
system для идентификации.

Режимы работы:
    MODE=rtsp       — живой RTSP-поток
    MODE=local      — локальный видеофайл

Требования:
    pip install opencv-python-headless requests pillow numpy

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
from io import BytesIO
from PIL import Image
from pathlib import Path

# Конфигурация

CONFIG = {
    # 'rtsp' или 'local'
    'MODE': os.getenv('MODE', 'local'),

    # RTSP-поток домофона
    'RTSP_URL': os.getenv(
        'RTSP_URL',
        'rtsp://admin:microimpuls25@192.168.88.211:554/Streaming/Channels/101'
    ),

    # Путь к локальному видео
    'VIDEO_PATH': os.getenv('VIDEO_PATH', 'C:/Users/asdera649/Videos/1.mp4'),

    # URL системы
    'API_BASE_URL': os.getenv('API_BASE_URL', 'http://127.0.0.1:8000'),

    # Токен домофона
    'SERVICE_TOKEN': os.getenv('SERVICE_TOKEN', 'токен-домофона'),

    # Сколько секунд ждать между попытками идентификации
    'IDENTIFY_INTERVAL_SEC': float(os.getenv('IDENTIFY_INTERVAL', '1.5')),

    # После успешного открытия — задержка перед следующей идентификацией
    'SUCCESS_COOLDOWN_SEC': float(os.getenv('SUCCESS_COOLDOWN', '10.0')),

    # Минимальный размер лица в пикселях (фильтрует далёкие/мелкие лица)
    'MIN_FACE_SIZE_PX': int(os.getenv('MIN_FACE_SIZE', '60')),

    # Минимальный score уверенности детектора (0.0–1.0)
    'MIN_CONFIDENCE': float(os.getenv('MIN_CONFIDENCE', '0.75')),

    # Отступ вокруг лица при вырезании (доля от размера лица)
    'FACE_PADDING': float(os.getenv('FACE_PADDING', '0.30')),

    # Качество JPEG при отправке в API в процентах
    'JPEG_QUALITY': int(os.getenv('JPEG_QUALITY', '100')),

    # Таймаут HTTP-запроса к API в секундах
    'API_TIMEOUT_SEC': int(os.getenv('API_TIMEOUT', '10')),

    # Логировать кадры без лица?
    'LOG_NO_FACE': False,

    # Задержка между переподключениями RTSP в секундах
    'RECONNECT_DELAY_SEC': int(os.getenv('RECONNECT_DELAY', '5')),
}

# Логирование

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('doorbell')


# Детектор лиц — YuNet (OpenCV >= 4.5.4)

class FaceDetector:
    """
    Детектор лиц YuNet
    """

    def __init__(self, input_size=(320, 240)):
        self.input_size = input_size

        model_path = Path(__file__).parent / "models" / "face_detection_yunet_2023mar.onnx"

        if not model_path.exists():
            raise FileNotFoundError(
                f"Модель YuNet не найдена: {model_path}\n"
            )

        self._detector = cv2.FaceDetectorYN.create(
            model=str(model_path),
            config='',
            input_size=self.input_size,
            score_threshold=CONFIG['MIN_CONFIDENCE'],
            nms_threshold=0.3,
            top_k=5,
        )
        log.info(
            f'Инициализирован YuNet (score_threshold={CONFIG["MIN_CONFIDENCE"]}, '
            f'min_size={CONFIG["MIN_FACE_SIZE_PX"]}px, model={model_path})'
        )

    def detect(self, frame_bgr: np.ndarray) -> list[dict]:
        """
        Возвращает список обнаруженных лиц.

        Каждое лицо:
            bbox   : (x, y, w, h) — bounding box
            score  : float        — уверенность детектора(0 - 1)
            landmarks: list[(x,y)] — 5 точек
        """
        h, w = frame_bgr.shape[:2]

        # YuNet требует явно задать размер входного изображения
        if (w, h) != self.input_size:
            self.input_size = (w, h)
            self._detector.setInputSize(self.input_size)

        _, raw = self._detector.detect(frame_bgr)
        if raw is None:
            return []

        results = []
        for det in raw:
            x, y, bw, bh = int(det[0]), int(det[1]), int(det[2]), int(det[3])
            score = float(det[14])

            # Фильтрация по минимальному размеру
            if bw < CONFIG['MIN_FACE_SIZE_PX'] or bh < CONFIG['MIN_FACE_SIZE_PX']:
                continue

            landmarks = [
                (int(det[4]),  int(det[5])),   # левый глаз
                (int(det[6]),  int(det[7])),   # правый глаз
                (int(det[8]),  int(det[9])),   # нос
                (int(det[10]), int(det[11])),  # левый уголок рта
                (int(det[12]), int(det[13])),  # правый уголок рта
            ]

            results.append({
                'bbox': (x, y, bw, bh),
                'score': score,
                'landmarks': landmarks,
            })

        return results

    def crop_best(self, frame_bgr: np.ndarray, faces: list[dict]):
        """
        Вырезает лицо с наибольшим score,
        добавляет отступ вокруг bbox
        """
        if not faces:
            return None

        best = max(faces, key=lambda f: f['score'])
        x, y, bw, bh = best['bbox']
        pad = int(max(bw, bh) * CONFIG['FACE_PADDING'])

        H, W = frame_bgr.shape[:2]
        x1 = max(0, x - pad)
        y1 = max(0, y - pad)
        x2 = min(W, x + bw + pad)
        y2 = min(H, y + bh + pad)

        return frame_bgr[y1:y2, x1:x2]


# Вспомогательные функции

def encode_frame_to_base64(frame_bgr: np.ndarray) -> str:
    """Конвертирует BGR-кадр OpenCV в base64 JPEG для отправки в API"""
    frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    img = Image.fromarray(frame_rgb)
    buf = BytesIO()
    img.save(buf, format='JPEG', quality=CONFIG['JPEG_QUALITY'])
    b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
    return f'data:image/jpeg;base64,{b64}'


def call_identify_api(image_data: str) -> dict:
    """Отправляет кадр в /biometric/api/identify/ и возвращает ответ."""
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


def handle_api_result(result: dict) -> None:
    """Логирует результат идентификации."""
    if result.get('identified'):
        user   = result.get('full_name') or result.get('username', '?')
        conf   = result.get('recognition_confidence', 0) * 100
        opened = result.get('door_opened', False)
        log.info(f'Успешно: {user} (conf={conf:.1f}%, door_opened={opened})')
    else:
        conf  = result.get('recognition_confidence', 0) * 100
        error = result.get('error', 'unknown')
        log.info(f'Доступ отклонён: {error} (best_conf={conf:.1f}%)')


# Логика обработки кадров

def process_stream(cap: cv2.VideoCapture, detector: FaceDetector) -> bool:
    """
    Основной цикл обработки кадров

    Возвращает:
        True — нужно переподключиться (ошибки чтения)
        False — поток завершился штатно (конец файла)
    """
    last_identify_time = 0.0
    last_success_time  = 0.0
    consecutive_errors = 0

    while True:
        ret, frame = cap.read()

        if not ret:
            consecutive_errors += 1
            if consecutive_errors > 10:
                log.warning('Много ошибок чтения кадров')
                return True   # попытаться переподключиться
            time.sleep(0.05)
            continue

        consecutive_errors = 0
        now = time.time()

        # Задержка после успешного открытия
        if now - last_success_time < CONFIG['SUCCESS_COOLDOWN_SEC']:
            continue

        # Интервал между идентификациями
        if now - last_identify_time < CONFIG['IDENTIFY_INTERVAL_SEC']:
            continue

        # Обнаружение лиц
        faces = detector.detect(frame)

        if not faces:
            if CONFIG['LOG_NO_FACE']:
                log.debug('Лиц в кадре не обнаружено')
            continue

        scores_str = ', '.join(f'{f["score"]:.2f}' for f in faces)
        log.info(f'Обнаружено лиц: {len(faces)} (score: {scores_str})')

        # Вырезаем лучшее лицо
        face_crop = detector.crop_best(frame, faces)
        if face_crop is None or face_crop.size == 0:
            continue

        # Отправка в API
        image_data = encode_frame_to_base64(face_crop)
        last_identify_time = now

        log.info('Отправка в API...')
        result = call_identify_api(image_data)
        handle_api_result(result)

        if result.get('identified'):
            last_success_time = now


# Режим 1: RTSP-поток

def run_rtsp_mode(detector: FaceDetector) -> None:
    """Цикл подключения к RTSP и обработки потока"""
    delay = CONFIG['RECONNECT_DELAY_SEC']
    log.info(f'Режим: RTSP | URL: {CONFIG["RTSP_URL"]}')

    while True:
        cap = cv2.VideoCapture(CONFIG['RTSP_URL'])
        if not cap.isOpened():
            log.error(f'Не удалось открыть RTSP. Повтор через {delay}с...')
            time.sleep(delay)
            continue

        log.info('RTSP поток успешно открыт')
        process_stream(cap, detector)

        cap.release()
        log.info(f'Переподключение через {delay}с...')
        time.sleep(delay)


# Режим 2: локальный видеофайл

def run_local_mode(detector: FaceDetector) -> None:
    """
    Обрабатывает локальный видеофайл
    """
    path = CONFIG['VIDEO_PATH']
    if not path:
        log.error('VIDEO_PATH не задан. Установите переменную окружения VIDEO_PATH.')
        return

    if not os.path.isfile(path):
        log.error(f'Файл не найден: {path}')
        return

    log.info(f'Режим: локальный файл | Путь: {path}')

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        log.error(f'Не удалось открыть файл: {path}')
        return

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    frame_delay = 1.0 / fps
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration_sec = total_frames / fps

    log.info(
        f'Видео: {total_frames} кадров, {fps:.1f} FPS, '
        f'длительность {duration_sec:.1f}с'
    )

    # Оборачиваем в цикл с задержкой между кадрами
    last_identify_time = 0.0
    last_success_time  = 0.0

    while True:
        frame_start = time.time()

        ret, frame = cap.read()
        if not ret:
            log.info('Конец файла. Обработка завершена.')
            break

        now = time.time()

        if now - last_success_time < CONFIG['SUCCESS_COOLDOWN_SEC']:
            _sleep_remainder(frame_start, frame_delay)
            continue

        if now - last_identify_time < CONFIG['IDENTIFY_INTERVAL_SEC']:
            _sleep_remainder(frame_start, frame_delay)
            continue

        # Обнаружение лиц
        faces = detector.detect(frame)

        if not faces:
            if CONFIG['LOG_NO_FACE']:
                log.debug('Лиц в кадре не обнаружено')
            _sleep_remainder(frame_start, frame_delay)
            continue

        scores_str = ', '.join(f'{f["score"]:.2f}' for f in faces)
        log.info(f'Обнаружено лиц: {len(faces)} (score: {scores_str})')

        face_crop = detector.detect and detector.crop_best(frame, faces)
        if face_crop is None or face_crop.size == 0:
            _sleep_remainder(frame_start, frame_delay)
            continue

        # Отправка в API
        image_data = encode_frame_to_base64(face_crop)
        last_identify_time = now

        log.info('Отправка в API...')
        result = call_identify_api(image_data)
        handle_api_result(result)

        if result.get('identified'):
            last_success_time = now

        _sleep_remainder(frame_start, frame_delay)

    cap.release()


def _sleep_remainder(frame_start: float, frame_delay: float) -> None:
    """Спать до следующего кадра"""
    elapsed = time.time() - frame_start
    sleep_time = frame_delay - elapsed
    if sleep_time > 0:
        time.sleep(sleep_time)


# Точка входа

def main() -> None:
    mode = CONFIG['MODE'].lower()
    log.info(f'Запуск | Режим: {mode}')

    detector = FaceDetector()

    if mode == 'rtsp':
        run_rtsp_mode(detector)
    elif mode == 'local':
        run_local_mode(detector)
    else:
        log.error(f'Неизвестный режим: {mode!r}. Доступны: rtsp, local')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        log.info('Сервис остановлен')