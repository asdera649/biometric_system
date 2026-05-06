"""
apps/biometric/liveness/passive.py

Пассивный детектор живости.

TextureAnalysisDetector — эвристический, без внешней модели.
  Анализирует: LBP-текстуру, частотный спектр (FFT), контраст, градиенты.
  Реальное лицо: высокая текстурная сложность + богатый высокочастотный спектр.
  Фото/экран: сниженная вариативность, «плоский» спектр.

PluggableModelDetector — заглушка для обученной модели.
  Унаследовать и реализовать _load_model() / _predict_raw().
"""

from __future__ import annotations
import logging
import numpy as np
from PIL import Image
from .base import PassiveLivenessDetector, PassiveLivenessResult

logger = logging.getLogger('apps.biometric.liveness.passive')


class TextureAnalysisDetector(PassiveLivenessDetector):
    """
    Эвристический пассивный детектор на основе анализа текстуры и спектра.
    Не требует обученных весов — работает сразу.

    Применяется как базовая линия (baseline) или когда обученная
    модель ещё не готова.

    Компоненты оценки:
        lbp_variance (35%) — локальная текстурная вариативность (LBP-подобная)
        frequency (30%) — доля высокочастотного содержания (FFT)
        local_contrast(20%) — дисперсия оператора Лапласа
        gradient (15%) — богатство градиентов (Собель)
    """

    def __init__(self, threshold: float = 0.45):
        self._threshold = threshold

    @property
    def name(self) -> str:
        return 'TextureAnalysis-v1'

    def predict(self, image: Image.Image) -> PassiveLivenessResult:
        try:
            import cv2
            img_array = np.array(image.convert('RGB'))
            gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)

            scores = {
                'lbp':      self._lbp_variance_score(gray),
                'frequency': self._frequency_score(gray),
                'contrast': self._local_contrast_score(gray, cv2),
                'gradient': self._gradient_score(gray, cv2),
            }

            final = (
                0.35 * scores['lbp'] +
                0.30 * scores['frequency'] +
                0.20 * scores['contrast'] +
                0.15 * scores['gradient']
            )
            final = float(np.clip(final, 0.0, 1.0))

            logger.debug(
                f'[TextureAnalysis] score={final:.3f} '
                f'lbp={scores["lbp"]:.2f} freq={scores["frequency"]:.2f} '
                f'contrast={scores["contrast"]:.2f} grad={scores["gradient"]:.2f}'
            )

            return PassiveLivenessResult(
                score=final,
                is_live=final >= self._threshold,
                confidence=min(abs(final - 0.5) * 2, 1.0),
                details=scores,
            )

        except ImportError:
            # Fallback без OpenCV: только яркостные характеристики
            return self._fallback_numpy(image)
        except Exception as e:
            logger.error(f'TextureAnalysisDetector.predict error: {e}')
            # Fail-open: сомнительный случай уйдёт на active challenge
            return PassiveLivenessResult(
                score=0.5, is_live=True, confidence=0.0,
                details={'error': str(e)},
            )

    # Компоненты

    def _lbp_variance_score(self, gray: np.ndarray) -> float:
        """
        Средняя локальная дисперсия патчей.
        Реальное лицо → высокая дисперсия из-за поровых каналов,
        волос, текстуры кожи. Фото на экране → сниженная.
        """
        h, w = gray.shape
        ps = 16  # размер патча
        step = ps // 2
        variances = [
            gray[y:y+ps, x:x+ps].astype(np.float32).var()
            for y in range(0, h - ps, step)
            for x in range(0, w - ps, step)
        ]
        if not variances:
            return 0.5
        mean_var = float(np.mean(variances))
        # Эмпирически: живое > 300, фото < 100
        return float(np.clip(mean_var / 450.0, 0.0, 1.0))

    def _frequency_score(self, gray: np.ndarray) -> float:
        """
        Доля высокочастотного содержания в спектре Фурье.
        Реальная кожа даёт богатый спектр; экран/распечатка — «срезает» верхние частоты.
        """
        fshift = np.fft.fftshift(np.fft.fft2(gray.astype(np.float32)))
        magnitude = np.abs(fshift)

        h, w = magnitude.shape
        cy, cx = h // 2, w // 2
        y_idx, x_idx = np.ogrid[:h, :w]
        dist = np.sqrt((y_idx - cy) ** 2 + (x_idx - cx) ** 2)
        max_dist = np.hypot(cy, cx)

        high_freq_mask = dist >= max_dist * 0.30  # внешние 70% радиуса
        total = magnitude.sum()
        if total < 1e-6:
            return 0.5

        ratio = float(magnitude[high_freq_mask].sum() / total)
        # Живое: ~0.88–0.95; фото/экран: ~0.70–0.82
        return float(np.clip((ratio - 0.70) / (0.95 - 0.70), 0.0, 1.0))

    def _local_contrast_score(self, gray: np.ndarray, cv2) -> float:
        """Дисперсия лапласиана — мера локальной резкости и микроконтраста."""
        lap = cv2.Laplacian(gray, cv2.CV_64F)
        var = float(lap.var())
        return float(np.clip(var / 800.0, 0.0, 1.0))

    def _gradient_score(self, gray: np.ndarray, cv2) -> float:
        """Относительная насыщенность градиентов."""
        gx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
        gy = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
        mag = float(np.sqrt(gx ** 2 + gy ** 2).mean())
        mean_brightness = float(gray.mean()) or 1.0
        norm = mag / mean_brightness
        return float(np.clip(norm / 0.45, 0.0, 1.0))

    def _fallback_numpy(self, image: Image.Image) -> PassiveLivenessResult:
        """Минимальная эвристика без OpenCV."""
        arr = np.array(image.convert('L'), dtype=np.float32)
        brightness = float(arr.mean() / 255.0)
        b_score = max(0.0, 1.0 - abs(brightness - 0.45) * 2.0)
        contrast = min(float(arr.std()) / 60.0, 1.0)
        score = 0.5 * b_score + 0.5 * contrast
        return PassiveLivenessResult(
            score=float(np.clip(score, 0.0, 1.0)),
            is_live=score >= self._threshold,
            confidence=0.2,  # низкая уверенность без opencv
            details={'fallback': True},
        )


class PluggableModelDetector(PassiveLivenessDetector):
    """
    Базовый класс для подключения произвольной обученной модели.

    Пример подключения:

    class MyAntiSpoofNet(PluggableModelDetector):

        def _load_model(self):
            import torch
            self._model = torch.load(self.model_path, map_location='cpu')
            self._model.eval()

        def _predict_raw(self, image: Image.Image) -> float:
            import torch, torchvision.transforms as T
            t = T.Compose([T.Resize((128, 128)), T.ToTensor(),
                           T.Normalize([0.5]*3, [0.5]*3)])
            with torch.no_grad():
                logit = self._model(t(image).unsqueeze(0))
                return float(logit.sigmoid())

    # Активация без перезапуска сервера:
    from apps.biometric.liveness.pipeline import get_pipeline
    get_pipeline().swap_passive_detector(
        MyAntiSpoofNet('/path/to/weights.pth', threshold=0.55)
    )
    """

    def __init__(self, model_path: str, threshold: float = 0.50):
        self.model_path = model_path
        self._threshold = threshold
        self._model = None
        self._ready = False
        try:
            self._load_model()
            self._ready = True
            logger.info(f'PluggableModelDetector loaded: {model_path}')
        except NotImplementedError:
            pass  # subclass not yet implemented
        except Exception as e:
            logger.error(f'PluggableModelDetector load failed: {e}')

    def _load_model(self):
        """Переопределить: загрузка весов модели."""
        raise NotImplementedError

    def _predict_raw(self, image: Image.Image) -> float:
        """Переопределить: инференс → float 0.0–1.0 (1 = живой)."""
        raise NotImplementedError

    @property
    def name(self) -> str:
        import os
        return f'PluggableModel({os.path.basename(self.model_path)})'

    def predict(self, image: Image.Image) -> PassiveLivenessResult:
        if not self._ready:
            logger.warning('PluggableModelDetector not ready, falling back to fail-open')
            return PassiveLivenessResult(
                score=0.5, is_live=True, confidence=0.0,
                details={'ready': False},
            )
        try:
            score = float(np.clip(self._predict_raw(image), 0.0, 1.0))
            return PassiveLivenessResult(
                score=score,
                is_live=score >= self._threshold,
                confidence=min(abs(score - 0.5) * 2, 1.0),
                details={'model': self.name, 'raw': score},
            )
        except Exception as e:
            logger.error(f'PluggableModelDetector.predict error: {e}')
            return PassiveLivenessResult(
                score=0.5, is_live=True, confidence=0.0,
                details={'error': str(e)},
            )
