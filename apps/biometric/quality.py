"""
Оценка качества изображения лица.
Метрика 2 системы: комплексный показатель качества.
"""
import numpy as np
from PIL import Image


def calculate_quality(img: Image.Image) -> float:
    """
    Комплексная оценка качества изображения лица (0.0–1.0).
    Включает: резкость (Лапласиан), яркость, контраст, разрешение.
    """
    try:
        import cv2
        img_array = np.array(img.convert('RGB'))
        gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)

        # 1. Резкость — дисперсия лапласиана
        laplacian_var = cv2.Laplacian(gray, cv2.CV_64F).var()
        sharpness = min(laplacian_var / 800.0, 1.0)

        # 2. Яркость — близость среднего к 0.5 диапазона
        brightness = gray.mean() / 255.0
        brightness_score = 1.0 - abs(brightness - 0.45) * 1.8
        brightness_score = max(0.0, min(brightness_score, 1.0))

        # 3. Контраст — нормированное стд. отклонение
        contrast = min(gray.std() / 80.0, 1.0)

        # 4. Разрешение
        w, h = img.size
        res_score = min((w * h) / (640 * 480), 1.0)

        # Взвешенная сумма
        quality = (0.45 * sharpness + 0.25 * brightness_score +
                   0.20 * contrast + 0.10 * res_score)
        return float(round(min(max(quality, 0.0), 1.0), 4))

    except Exception:
        # Fallback без OpenCV
        try:
            img_array = np.array(img.convert('L'), dtype=float)
            brightness = img_array.mean() / 255.0
            brightness_score = 1.0 - abs(brightness - 0.45) * 1.8
            contrast = min(img_array.std() / 80.0, 1.0)
            return float(round(max(0.0, 0.5 * brightness_score + 0.5 * contrast), 4))
        except Exception:
            return 0.5
