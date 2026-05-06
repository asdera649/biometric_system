"""
apps/biometric/liveness/session.py

Серверное управление сеансом активного challenge.

Сеанс хранится в Django-сессии.
Содержит: тип задания, эталонный вектор текущего сеанса (для same-person),
данные распознавания первого кадра (для финального лога), счётчики.

Ключ в сессии: SESSION_KEY
Таймаут: CHALLENGE_TIMEOUT_SEC
"""

from __future__ import annotations
import time
import logging
import numpy as np
from typing import Optional

logger = logging.getLogger('apps.biometric.liveness.session')

SESSION_KEY          = 'liveness_challenge'
CHALLENGE_TIMEOUT_SEC = 45   # секунд на прохождение challenge


def create(
    request,
    *,
    username: str,
    challenge_type: str,
    reference_embedding: np.ndarray,  # вектор из первого кадра auth
    recognition_confidence: float,    # метрика распознавания (для финального лога)
    quality_score: float,
) -> None:
    """Создать новый сеанс challenge в Django-сессии."""
    request.session[SESSION_KEY] = {
        'username':              username,
        'challenge_type':        challenge_type,
        'reference_embedding':   reference_embedding.tolist(),
        'recognition_confidence': recognition_confidence,
        'quality_score':         quality_score,
        'created_at':            time.time(),
        'frames_analyzed':       0,
        'identity_failures':     0,   # счётчик реальных провалов (не −1.0)
        'completed':             False,
    }
    request.session.modified = True
    logger.info(f'Liveness session created: user={username} challenge={challenge_type}')


def get(request) -> Optional[dict]:
    """
    Получить активный сеанс.
    Возвращает None если сеанса нет или он истёк (и очищает его).
    """
    data = request.session.get(SESSION_KEY)
    if data is None:
        return None

    if time.time() - data['created_at'] > CHALLENGE_TIMEOUT_SEC:
        clear(request)
        logger.warning('Liveness session expired')
        return None

    return data


def increment_frame(request, identity_real_failure: bool) -> dict:
    """
    Увеличить счётчик кадров. Если реальный провал личности — увеличить счётчик.
    Возвращает обновлённые данные сеанса.
    """
    data = request.session.get(SESSION_KEY, {})
    data['frames_analyzed'] = data.get('frames_analyzed', 0) + 1
    if identity_real_failure:
        data['identity_failures'] = data.get('identity_failures', 0) + 1
    request.session[SESSION_KEY] = data
    request.session.modified = True
    return data


def mark_completed(request) -> None:
    """Пометить challenge как выполненный."""
    data = request.session.get(SESSION_KEY, {})
    data['completed'] = True
    request.session[SESSION_KEY] = data
    request.session.modified = True


def clear(request) -> None:
    """Удалить сеанс."""
    request.session.pop(SESSION_KEY, None)
    request.session.modified = True


def get_reference_embedding(session_data: dict) -> Optional[np.ndarray]:
    """Восстановить эталонный вектор из данных сеанса."""
    raw = session_data.get('reference_embedding')
    if raw is None:
        return None
    return np.array(raw, dtype=np.float32)


def seconds_remaining(session_data: dict) -> int:
    elapsed = time.time() - session_data.get('created_at', time.time())
    return max(0, int(CHALLENGE_TIMEOUT_SEC - elapsed))
