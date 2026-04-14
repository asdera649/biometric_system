"""
webhook_emulator.py — Эмулятор

Принимает вебхук от biometric-системы, проверяет подпись
и эмулирует открытие двери.

Запуск:
    python webhook_emulator.py
"""

import hashlib
import hmac
import logging
import os
import time
import requests
from requests.auth import HTTPDigestAuth
from datetime import datetime

from flask import Flask, request, jsonify

# Конфиг

SECRET         = os.getenv('WEBHOOK_SECRET', 'webhook-token')
HOST           = os.getenv('HOST', '0.0.0.0')
PORT           = int(os.getenv('PORT', 5050))
TIMESTAMP_TTL  = int(os.getenv('TIMESTAMP_TTL', 30))

# Логирование

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('webhook_emulator')

app = Flask(__name__)


def verify_signature(request_timestamp: str, signature: str) -> bool:
    """Проверяет подпись запроса"""
    # Временная метка не старше TIMESTAMP_TTL сек
    try:
        req_time = int(request_timestamp)
        if abs(time.time() - req_time) > TIMESTAMP_TTL:
            log.warning(f'Устаревшая метка времени: {request_timestamp}')
            return False
    except (ValueError, TypeError):
        return False

    expected = hmac.new(
        SECRET.encode(),
        request_timestamp.encode(),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature)


# Логика открытия

def open_door(ip: str, user: str, password: str, door_id: int = 1) -> bool:
    url = f"http://{ip}/ISAPI/AccessControl/RemoteControl/door/{door_id}"
    headers = {"Content-Type": "application/xml; charset=UTF-8"}
    payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<RemoteControlDoor>
    <cmd>open</cmd>
</RemoteControlDoor>"""

    try:
        log.info(f"Отправка команды открытия двери: {url}")

        resp = requests.put(
            url,
            data=payload.encode("utf-8"),
            auth=HTTPDigestAuth(user, password),
            headers=headers,
            timeout=3
        )

        if resp.status_code == 200:
            if b"<statusCode>1</statusCode>" in resp.content or b"<statusString>OK</statusString>" in resp.content:
                log.info("Дверь открыта (подтверждено устройством)")
                return True
            else:
                log.warning(f"Устройство ответило, но без подтверждения успеха: {resp.content[:200]}")
                return False
        else:
            log.error(f"HTTP {resp.status_code}: {resp.content[:200]}")
            return False

    except requests.exceptions.Timeout:
        log.error(f"Таймаут при подключении к {ip}")
        return False
    except requests.exceptions.ConnectionError:
        log.error(f"Ошибка подключения к {ip}")
        return False
    except requests.exceptions.RequestException as e:
        log.error(f"Ошибка открытия двери: {type(e).__name__}: {e}")
        return False
    except Exception as e:
        log.exception(f"Неожиданная ошибка: {e}")
        return False


@app.route('/webhook/open', methods=['POST'])
def webhook_open():
    """
    Принимает команду открытия двери от biometric-системы.

    Ожидаемые заголовки:
        X-Webhook-Secret     — простой токен
        X-Webhook-Signature  — HMAC-SHA256
        X-Webhook-Timestamp  — Unix timestamp

    Body JSON:
        {"action": "open", "timestamp": "1234567890"}

    Ответ:
        {"status": "ok",    "message": "Door opened"}
        {"status": "error", "message": "..."}
    """

    # Простая проверка токена
    incoming_secret = request.headers.get('X-Webhook-Secret', '')
    if incoming_secret != SECRET:
        log.warning(f'Неверный токен от {request.remote_addr}')
        return jsonify({'status': 'error', 'message': 'Unauthorized'}), 401

    # HMAC-проверка
    signature = request.headers.get('X-Webhook-Signature', '')
    timestamp = request.headers.get('X-Webhook-Timestamp', '')

    if signature and timestamp:
        if not verify_signature(timestamp, signature):
            log.warning(f'Неверная подпись от {request.remote_addr}')
            return jsonify({'status': 'error', 'message': 'Invalid signature'}), 401
    else:
        log.debug('HMAC-подпись не передана используется только токен')

    # Парсинг
    data = request.get_json(silent=True) or {}
    action = data.get('action', '')

    if action != 'open':
        log.warning(f'Неизвестное действие: {action!r}')
        return jsonify({'status': 'error', 'message': f'Unknown action: {action}'}), 400

    # Открываем дверь
    log.info(
        f'Команда от {request.remote_addr} | '
        f'action={action} | ts={data.get("timestamp")} | '
        f'time={datetime.now():%H:%M:%S}'
    )

    success = open_door("192.168.88.211", "admin", "microimpuls25")

    if success:
        return jsonify({'status': 'ok', 'message': 'Door opened'}), 200
    else:
        return jsonify({'status': 'error', 'message': 'Failed to open door'}), 500


@app.route('/health', methods=['GET'])
def health():
    """Проверка доступности сервиса"""
    return jsonify({'status': 'ok', 'service': 'webhook_emulator', 'time': time.time()}), 200


if __name__ == '__main__':
    log.info(f'Запуск эмулятора на http://{HOST}:{PORT}')
    app.run(host=HOST, port=PORT, debug=False)