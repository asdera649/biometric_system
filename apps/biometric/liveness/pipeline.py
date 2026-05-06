"""
apps/biometric/liveness/pipeline.py

Гибридный конвейер liveness detection:
    Этап 1 (пассивный) → Этап 2 (активный challenge, если нужен)

На каждом кадре активного этапа параллельно проверяется:
    - выполнение задания
    - что перед камерой тот же человек

Конвейер спроектирован для горячей замены компонентов без перезапуска:
    pipeline = get_pipeline()
    pipeline.swap_passive_detector(MyNewModel())
    pipeline.swap_challenge_engine(MyLSTMEngine())
"""

from __future__ import annotations
import logging
import random
import numpy as np
from PIL import Image
from typing import Optional

from .base import (
    PassiveLivenessDetector, ActiveChallengeEngine,
    ChallengeFrameResult,
)
from .passive import TextureAnalysisDetector
from .active import MediaPipeChallengeEngine

logger = logging.getLogger('apps.biometric.liveness.pipeline')

_pipeline: Optional['HybridLivenessPipeline'] = None

def get_pipeline() -> 'HybridLivenessPipeline':
    global _pipeline
    if _pipeline is None:
        _pipeline = HybridLivenessPipeline()
    return _pipeline


class HybridLivenessPipeline:
    """
    Двухэтапный гибридный конвейер живости.

    Пороги пассивного этапа:
        score ≥ PASSIVE_ACCEPT  → пропустить без challenge
        score ≤ PASSIVE_REJECT  → жёсткий отказ
        иначе                   → запустить активный challenge

    Порог верификации личности (same-person):
        IDENTITY_SIM_THRESHOLD — минимальная косинусная схожесть.
        Если лицо не обнаружено чётко (confidence низкое — норма при повороте),
        возвращается score = −1.0 и ошибки личности не считаются.
    """

    PASSIVE_ACCEPT_THRESHOLD  = 0.68   # выше → challenge не нужен
    PASSIVE_REJECT_THRESHOLD  = 0.22   # ниже → жёсткий отказ
    IDENTITY_SIM_THRESHOLD    = 0.52   # min схожесть с эталоном текущего сеанса
    FACE_DET_MIN_CONFIDENCE   = 0.75   # нижний порог детекции при same-person

    # Максимум подряд идущих провалов верификации личности (не −1.0)
    MAX_IDENTITY_FAILURES = 3

    def __init__(
        self,
        passive_detector: Optional[PassiveLivenessDetector] = None,
        challenge_engine: Optional[ActiveChallengeEngine] = None,
    ):
        self.passive  = passive_detector or TextureAnalysisDetector(threshold=0.45)
        self.engine   = challenge_engine or MediaPipeChallengeEngine()
        logger.info(
            f'HybridLivenessPipeline init: '
            f'passive={self.passive.name}, active={self.engine.name}'
        )

    def swap_passive_detector(self, detector: PassiveLivenessDetector) -> None:
        """Заменить пассивный детектор без перезапуска."""
        old = self.passive.name
        self.passive = detector
        logger.info(f'Passive detector swapped: {old} → {detector.name}')

    def swap_challenge_engine(self, engine: ActiveChallengeEngine) -> None:
        """Заменить движок активного challenge без перезапуска."""
        old = self.engine.name
        self.engine = engine
        logger.info(f'Challenge engine swapped: {old} → {engine.name}')

    # Этап 1: Пассивная проверка

    def run_passive(self, image: Image.Image) -> dict:
        """
        Запустить пассивный детектор.

        Returns:
            status      : 'accept' | 'challenge' | 'reject'
            score       : float 0–1
            confidence  : float 0–1
            detector    : str
            details     : dict
        """
        result = self.passive.predict(image)

        if result.score >= self.PASSIVE_ACCEPT_THRESHOLD:
            status = 'accept'
        elif result.score <= self.PASSIVE_REJECT_THRESHOLD:
            status = 'reject'
        else:
            status = 'challenge'

        logger.info(
            f'[Passive/{self.passive.name}] '
            f'score={result.score:.3f} → {status}'
        )
        return {
            'status':     status,
            'score':      result.score,
            'is_live':    result.is_live,
            'confidence': result.confidence,
            'detector':   self.passive.name,
            'details':    result.details,
        }

    def select_challenge(self) -> str:
        """Случайный тип задания из доступных."""
        return random.choice(ActiveChallengeEngine.ALL_CHALLENGES)

    # Этап 2: Анализ кадра активного challenge

    def analyze_challenge_frame(
        self,
        image: Image.Image,
        challenge_type: str,
        reference_embedding: np.ndarray,
        face_processor,           # FaceProcessor из apps.biometric.face_processor
    ) -> ChallengeFrameResult:
        """
        Анализирует кадр в ходе активного challenge.

        Параллельно:
          1. Проверяет выполнение задания (challenge_engine.analyze_frame).
          2. Верифицирует, что перед камерой тот же человек (same-person check).

        reference_embedding — вектор ТЕКУЩЕГО сеанса, извлечённый из первого
        кадра аутентификации (не хранимый шаблон). Это гарантирует, что
        человек, прошедший распознавание, и человек в challenge — одно лицо.

        identity_score == −1.0 означает «лицо не обнаружено чётко» — нормально
        при повороте/моргании, ошибкой не считается.
        """
        # 1. Задание
        ch_result = self.engine.analyze_frame(image, challenge_type)

        # 2. Верификация личности
        identity_score = self._same_person_check(
            image, reference_embedding, face_processor
        )

        # identity_ok: −1.0 (нет чёткого лица) тоже ОК — пользователь мог отвернуться
        identity_ok = (
            identity_score < 0  # лицо не обнаружено (нормально при повороте)
            or identity_score >= self.IDENTITY_SIM_THRESHOLD
        )

        logger.debug(
            f'[Challenge/{challenge_type}] '
            f'completed={ch_result["completed"]} '
            f'progress={ch_result.get("progress", 0):.2f} '
            f'identity={identity_score:.3f} ok={identity_ok}'
        )

        return ChallengeFrameResult(
            challenge_completed=bool(ch_result.get('completed', False)),
            progress=float(ch_result.get('progress', 0.0)),
            landmarks_detected=bool(ch_result.get('landmarks_detected', False)),
            identity_score=identity_score,
            identity_ok=identity_ok,
            details={
                'challenge': ch_result.get('details', {}),
                'identity_score': identity_score,
            },
        )

    def _same_person_check(
        self,
        image: Image.Image,
        reference_embedding: np.ndarray,
        face_processor,
    ) -> float:
        """
        Сравнивает текущий кадр с эталонным вектором сеанса.
        Возвращает косинусное сходство или −1.0 если лицо не обнаружено чётко.
        """
        try:
            face_tensor, det_confidence, _ = face_processor.detect_face(image)

            if face_tensor is None or det_confidence < self.FACE_DET_MIN_CONFIDENCE:
                # Лицо не обнаружено или низкая уверенность детекции.
                # При повороте/моргании это нормально — не считаем ошибкой.
                return -1.0

            current_emb = face_processor.get_embedding(face_tensor)
            if current_emb is None:
                return -1.0

            return float(face_processor.cosine_similarity(reference_embedding, current_emb))

        except Exception as e:
            logger.error(f'same_person_check error: {e}')
            return -1.0
