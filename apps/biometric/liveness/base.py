"""
apps/biometric/liveness/base.py

Абстрактные интерфейсы для компонентов liveness detection.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from PIL import Image


@dataclass
class PassiveLivenessResult:
    """Результат пассивной проверки живости."""
    score: float          # 0.0–1.0: вероятность, что лицо живое
    is_live: bool         # итоговое решение (score >= threshold)
    confidence: float     # уверенность решения: 0 = на пороге, 1 = максимально уверен
    details: dict = field(default_factory=dict)  # детали для логирования/отладки


@dataclass
class ChallengeFrameResult:
    """Результат анализа одного кадра в ходе активного challenge."""
    challenge_completed: bool     # действие выполнено в этом кадре
    progress: float               # 0.0–1.0 – прогресс к выполнению
    landmarks_detected: bool      # удалось ли найти лицо/точки
    identity_score: float         # косинусная схожесть с эталоном (−1 = нет лица)
    identity_ok: bool             # схожесть выше порога или лицо не обнаружено (норма при повороте)
    details: dict = field(default_factory=dict)


class PassiveLivenessDetector(ABC):
    """
    Абстрактный базовый класс для пассивных детекторов живости.

    # Подключение своей модели:
    from apps.biometric.liveness.pipeline import get_pipeline
    get_pipeline().swap_passive_detector(MyTrainedModel('weights.pth'))
    """

    @abstractmethod
    def predict(self, image: Image.Image) -> PassiveLivenessResult:
        """Анализирует один кадр. Возвращает PassiveLivenessResult."""
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """Название модели/метода для логирования."""
        pass

    @property
    def requires_multiple_frames(self) -> bool:
        """Переопределить, если модели нужно несколько кадров."""
        return False


class ActiveChallengeEngine(ABC):
    """
    Абстрактный базовый класс для активного challenge-response.

    Чтобы подключить свою модель:
        class MyLSTMChallenge(ActiveChallengeEngine): ...
        get_pipeline().swap_challenge_engine(MyLSTMChallenge())
    """

    # Типы заданий
    CHALLENGE_BLINK       = 'blink'
    CHALLENGE_TURN_LEFT   = 'turn_left'
    CHALLENGE_TURN_RIGHT  = 'turn_right'
    CHALLENGE_SMILE       = 'smile'

    ALL_CHALLENGES = [
        CHALLENGE_BLINK,
        CHALLENGE_TURN_LEFT,
        CHALLENGE_TURN_RIGHT,
        CHALLENGE_SMILE,
    ]

    CHALLENGE_LABELS = {
        CHALLENGE_BLINK:      'Моргните',
        CHALLENGE_TURN_LEFT:  'Медленно поверните голову влево',
        CHALLENGE_TURN_RIGHT: 'Медленно поверните голову вправо',
        CHALLENGE_SMILE:      'Улыбнитесь',
    }

    CHALLENGE_ICONS = {
        CHALLENGE_BLINK:      'eye',
        CHALLENGE_TURN_LEFT:  'arrow-left-circle',
        CHALLENGE_TURN_RIGHT: 'arrow-right-circle',
        CHALLENGE_SMILE:      'emoji-smile',
    }

    @abstractmethod
    def analyze_frame(self, image: Image.Image, challenge_type: str) -> dict:
        """
        Анализирует кадр на предмет выполнения задания.

        Возвращает dict:
            completed (bool): задание выполнено в этом кадре
            progress  (float): 0.0–1.0 прогресс
            landmarks_detected (bool): найдено ли лицо
            details (dict): дополнительно
        """
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        pass
