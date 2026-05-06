"""
apps/biometric/liveness/active.py

Активный challenge-response engine на основе MediaPipe Face Mesh.

Поддерживаемые задания:
    blink       — моргнуть (EAR: Eye Aspect Ratio)
    turn_left   — повернуть голову влево
    turn_right  — повернуть голову вправо
    smile       — улыбнуться (MAR: Mouth Aspect Ratio)
"""

from __future__ import annotations
import logging
import numpy as np
from PIL import Image
from .base import ActiveChallengeEngine

logger = logging.getLogger('apps.biometric.liveness.active')

_face_mesh = None

def _get_face_mesh():
    global _face_mesh
    if _face_mesh is None:
        try:
            import mediapipe as mp
            _face_mesh = mp.solutions.face_mesh.FaceMesh(
                static_image_mode=True,
                max_num_faces=1,
                refine_landmarks=True,
                min_detection_confidence=0.5,
                min_tracking_confidence=0.5,
            )
            logger.info('MediaPipe FaceMesh инициализирован')
        except ImportError:
            logger.error(
                'mediapipe не установлен! '
                'Выполни: pip install mediapipe --break-system-packages'
            )
        except Exception as e:
            logger.error(f'Ошибка инициализации MediaPipe: {e}')
    return _face_mesh


# Индексы точек MediaPipe Face Mesh 468
# Левый глаз (в кадре левая сторона): верхнее/нижнее веко + углы
LEFT_EYE_IDX  = [362, 385, 387, 263, 373, 380]
# Правый глаз
RIGHT_EYE_IDX = [33,  160, 158, 133, 153, 144]

NOSE_TIP   = 1
LEFT_EAR   = 234
RIGHT_EAR  = 454

MOUTH_LEFT  = 61
MOUTH_RIGHT = 291
UPPER_LIP   = 13
LOWER_LIP   = 14

# Пороги
EAR_BLINK_THRESH  = 0.22   # EAR < порог → моргание
TURN_ASYM_THRESH  = 0.18   # асимметрия носа > порог → поворот
SMILE_MAR_THRESH  = 0.30   # MAR > порог → улыбка


class MediaPipeChallengeEngine(ActiveChallengeEngine):
    """
    Активный движок заданий на базе MediaPipe Face Mesh.
    Использует геометрические соотношения: EAR, MAR, асимметрия носа.
    """

    @property
    def name(self) -> str:
        return 'MediaPipeFaceMesh-v1'

    def analyze_frame(self, image: Image.Image, challenge_type: str) -> dict:
        face_mesh = _get_face_mesh()
        if face_mesh is None:
            return self._unavailable()

        try:
            img_rgb = np.array(image.convert('RGB'))
            results = face_mesh.process(img_rgb)

            if not results.multi_face_landmarks:
                return {
                    'completed': False,
                    'progress': 0.0,
                    'landmarks_detected': False,
                    'details': {'reason': 'no_face'},
                }

            lm = results.multi_face_landmarks[0].landmark
            h, w = img_rgb.shape[:2]
            # pts: {idx: (x_px, y_px, z_norm)}
            pts = {i: (lm[i].x * w, lm[i].y * h, lm[i].z) for i in range(len(lm))}

            dispatch = {
                self.CHALLENGE_BLINK:      self._check_blink,
                self.CHALLENGE_TURN_LEFT:  lambda p: self._check_turn(p, 'left'),
                self.CHALLENGE_TURN_RIGHT: lambda p: self._check_turn(p, 'right'),
                self.CHALLENGE_SMILE:      self._check_smile,
            }
            handler = dispatch.get(challenge_type)
            if handler is None:
                return self._unavailable()

            result = handler(pts)
            result['landmarks_detected'] = True
            return result

        except Exception as e:
            logger.error(f'MediaPipeChallengeEngine.analyze_frame error: {e}')
            return {
                'completed': False, 'progress': 0.0,
                'landmarks_detected': False, 'details': {'error': str(e)},
            }

    def _check_blink(self, pts: dict) -> dict:
        """
        Eye Aspect Ratio (EAR).
        EAR = (||p2-p6|| + ||p3-p5||) / (2 * ||p1-p4||)
        При моргании EAR резко падает ниже порога.
        """
        left_ear  = self._ear(pts, LEFT_EYE_IDX)
        right_ear = self._ear(pts, RIGHT_EYE_IDX)
        avg_ear   = (left_ear + right_ear) / 2.0

        completed = avg_ear < EAR_BLINK_THRESH
        # Прогресс: 0 при avg_ear=0.35 (открыт), 1 при avg_ear=0.0 (закрыт)
        progress  = float(np.clip(1.0 - avg_ear / EAR_BLINK_THRESH, 0.0, 1.0))

        return {
            'completed': completed,
            'progress': 1.0 if completed else progress,
            'details': {
                'left_ear': round(left_ear, 3),
                'right_ear': round(right_ear, 3),
                'avg_ear': round(avg_ear, 3),
                'threshold': EAR_BLINK_THRESH,
            },
        }

    def _check_turn(self, pts: dict, direction: str) -> dict:
        """
        Асимметрия положения кончика носа относительно ушей.

        asym = (d_right - d_left) / (d_left + d_right)
         > 0 → нос ближе к левому уху → поворот влево (пользователь повернул голову налево)
         < 0 → нос ближе к правому уху → поворот вправо

        Примечание: MediaPipe landmark 234 — слева в кадре (ухо пользователя справа),
        landmark 454 — справа в кадре (ухо пользователя слева).
        При повороте налево нос сближается с landmark 234.
        """
        nose  = np.array(pts[NOSE_TIP][:2])
        l_ear = np.array(pts[LEFT_EAR][:2])
        r_ear = np.array(pts[RIGHT_EAR][:2])

        d_left  = np.linalg.norm(nose - l_ear)
        d_right = np.linalg.norm(nose - r_ear)
        total   = d_left + d_right

        if total < 5.0:
            return {'completed': False, 'progress': 0.0}

        asym = (d_right - d_left) / total  # [-1, +1]

        if direction == 'left':
            completed = asym > TURN_ASYM_THRESH
            progress  = float(np.clip(asym / TURN_ASYM_THRESH, 0.0, 1.0))
        else:
            completed = asym < -TURN_ASYM_THRESH
            progress  = float(np.clip(-asym / TURN_ASYM_THRESH, 0.0, 1.0))

        return {
            'completed': completed,
            'progress': 1.0 if completed else progress,
            'details': {
                'asym': round(float(asym), 3),
                'direction': direction,
                'threshold': TURN_ASYM_THRESH,
            },
        }

    def _check_smile(self, pts: dict) -> dict:
        """
        Mouth Aspect Ratio (MAR).
        MAR = вертикальное открытие / горизонтальная ширина рта.
        Высокий MAR → открытый рот/улыбка.
        """
        ml = np.array(pts[MOUTH_LEFT][:2])
        mr = np.array(pts[MOUTH_RIGHT][:2])
        ul = np.array(pts[UPPER_LIP][:2])
        ll = np.array(pts[LOWER_LIP][:2])

        width  = np.linalg.norm(mr - ml)
        opening = np.linalg.norm(ll - ul)

        if width < 5.0:
            return {'completed': False, 'progress': 0.0}

        mar = opening / width
        completed = mar > SMILE_MAR_THRESH
        progress  = float(np.clip(mar / SMILE_MAR_THRESH, 0.0, 1.0))

        return {
            'completed': completed,
            'progress': 1.0 if completed else progress,
            'details': {
                'mar': round(float(mar), 3),
                'threshold': SMILE_MAR_THRESH,
            },
        }

    #Утилиты

    @staticmethod
    def _ear(pts: dict, indices: list) -> float:
        """Eye Aspect Ratio по 6 точкам глаза."""
        p = [np.array(pts[i][:2]) for i in indices]
        v1 = np.linalg.norm(p[1] - p[5])
        v2 = np.linalg.norm(p[2] - p[4])
        h  = np.linalg.norm(p[0] - p[3])
        return float((v1 + v2) / (2.0 * h)) if h > 1e-6 else 0.3

    @staticmethod
    def _unavailable() -> dict:
        return {
            'completed': False, 'progress': 0.0,
            'landmarks_detected': False,
            'details': {'error': 'MediaPipe недоступен'},
        }
