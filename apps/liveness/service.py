import os
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import cv2
import numpy as np
import torch
import torch.nn.functional as F
from django.conf import settings

from src.model_lib.MiniFASNet import (
    MiniFASNetV1, MiniFASNetV2,
    MiniFASNetV1SE, MiniFASNetV2SE,
)
from src.data_io import transform as trans
from src.generate_patches import CropImage
from src.utility import get_kernel, parse_model_name

logger = logging.getLogger('apps.liveness')

MODEL_MAPPING = {
    'MiniFASNetV1':   MiniFASNetV1,
    'MiniFASNetV2':   MiniFASNetV2,
    'MiniFASNetV1SE': MiniFASNetV1SE,
    'MiniFASNetV2SE': MiniFASNetV2SE,
}

LABEL_NAMES = {0: 'fake', 1: 'real', 2: 'fake'}

@dataclass
class LivenessResult:
    is_real: bool
    score: float           # вероятность, что это живое лицо (0–1)
    label: int             # argmax-метка
    label_name: str        # 'real' | 'fake' | 'unknown'
    raw_scores: list       # softmax по всем классам
    face_bbox: Optional[list] = None   # [x, y, w, h] или None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            'is_real': self.is_real,
            'score': round(self.score, 4),
            'label': self.label,
            'label_name': self.label_name,
            'raw_scores': [round(float(s), 4) for s in self.raw_scores],
            'face_bbox': self.face_bbox,
            'error': self.error,
        }

class _RetinaFaceDetector:
    """Обёртка вокруг Caffe RetinaFace."""

    def __init__(self, caffemodel: str, prototxt: str, confidence: float = 0.6):
        self.detector = cv2.dnn.readNetFromCaffe(prototxt, caffemodel)
        self.confidence_threshold = confidence

    def get_bbox(self, img: np.ndarray) -> Optional[list]:
        """Возвращает [x, y, w, h] наиболее уверенного лица или None."""
        import math
        height, width = img.shape[:2]
        aspect_ratio = width / height

        blob_img = img
        if width * height >= 192 * 192:
            blob_img = cv2.resize(
                img,
                (int(192 * math.sqrt(aspect_ratio)),
                 int(192 / math.sqrt(aspect_ratio))),
                interpolation=cv2.INTER_LINEAR,
            )

        blob = cv2.dnn.blobFromImage(blob_img, 1, mean=(104, 117, 123))
        self.detector.setInput(blob, 'data')
        out = self.detector.forward('detection_out').squeeze()

        if out.ndim == 1:
            out = out[np.newaxis, :]

        valid = out[out[:, 2] >= self.confidence_threshold]
        if len(valid) == 0:
            return None

        best = valid[np.argmax(valid[:, 2])]
        left   = best[3] * width
        top    = best[4] * height
        right  = best[5] * width
        bottom = best[6] * height
        return [int(left), int(top), int(right - left + 1), int(bottom - top + 1)]

class LivenessService:
    """
    Singleton-сервис пассивной проверки живости.

    Использование:
        service = LivenessService.get_instance()
        result  = service.check(image_bytes_or_np_array)
    """

    _instance: Optional['LivenessService'] = None
    _lock = threading.Lock()

    def __init__(self):
        cfg = settings.LIVENESS_CONFIG
        device_id = cfg.get('DEVICE_ID', 0)
        self.device = torch.device(
            f'cuda:{device_id}' if torch.cuda.is_available() else 'cpu'
        )
        self.model_dir: str = cfg['MODEL_DIR']

        self.detector = _RetinaFaceDetector(
            caffemodel=cfg['DETECTOR_CAFFEMODEL'],
            prototxt=cfg['DETECTOR_PROTOTXT'],
        )
        self.image_cropper = CropImage()

        # model_path - (model, kernel_size)
        self.loaded_models: Dict[str, Tuple[torch.nn.Module, tuple]] = {}
        self._preload_models()

    # Singleton
    @classmethod
    def get_instance(cls) -> 'LivenessService':
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls):
        with cls._lock:
            cls._instance = None

    # Загрузка моделей
    def _preload_models(self):
        if not os.path.isdir(self.model_dir):
            logger.warning('Liveness: папка моделей не найдена: %s', self.model_dir)
            return

        loaded = 0
        for fname in sorted(os.listdir(self.model_dir)):
            if fname.endswith('.pth'):
                path = os.path.join(self.model_dir, fname)
                try:
                    self._load_model(path)
                    loaded += 1
                    logger.info('Liveness: загружена модель %s → %s', fname, self.device)
                except Exception as exc:
                    logger.error('Liveness: не удалось загрузить %s: %s', fname, exc)

        if loaded == 0:
            logger.warning(
                'Liveness: ни одна модель не загружена из %s. '
                'Проверьте наличие .pth файлов.', self.model_dir
            )

    def _load_model(self, model_path: str):
        if model_path in self.loaded_models:
            return

        model_name = os.path.basename(model_path)
        h_input, w_input, model_type, _ = parse_model_name(model_name)
        kernel_size = get_kernel(h_input, w_input)

        if model_type not in MODEL_MAPPING:
            raise ValueError(f'Liveness: неизвестный тип модели: {model_type}')

        model = MODEL_MAPPING[model_type](conv6_kernel=kernel_size).to(self.device)

        state_dict = torch.load(model_path, map_location=self.device)
        first_key = next(iter(state_dict))
        if 'module.' in first_key:
            state_dict = OrderedDict(
                (k[7:], v) for k, v in state_dict.items()
            )
        model.load_state_dict(state_dict)
        model.eval()

        self.loaded_models[model_path] = (model, kernel_size)

    # Инференс
    def _infer_one_model(self, model: torch.nn.Module, img_patch: np.ndarray) -> np.ndarray:
        """Прогон одной модели на кропе; возвращает softmax-вероятности."""
        transform = trans.Compose([trans.ToTensor()])
        tensor = transform(img_patch).unsqueeze(0).to(self.device)
        with torch.no_grad():
            logits = model(tensor)
            probs = F.softmax(logits, dim=1).cpu().numpy()
        return probs  # shape (1, num_classes)

    def _decode_image(self, image_input) -> Optional[np.ndarray]:
        """
        Принимает:
          np.ndarray  (BGR)
          bytes / bytearray
          file-like object (Django InMemoryUploadedFile и т.д.)
        Возвращает BGR numpy-массив или None при ошибке.
        """
        if isinstance(image_input, np.ndarray):
            return image_input

        if hasattr(image_input, 'read'):
            image_input = image_input.read()

        if isinstance(image_input, (bytes, bytearray)):
            arr = np.frombuffer(image_input, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            return img

        return None

    # API

    def check(self, image_input, threshold: float = 0.5) -> LivenessResult:
        """
        Пассивная проверка живости.

        Args:
            image_input: BGR np.ndarray, bytes или file-like object.
            threshold:   Минимальный score для признания лица живым.

        Returns:
            LivenessResult dataclass.
        """
        if not self.loaded_models:
            return LivenessResult(
                is_real=False, score=0.0, label=-1,
                label_name='unknown', raw_scores=[],
                error='No models loaded. Check LIVENESS_CONFIG["MODEL_DIR"].',
            )

        # 1. Декодирование
        img = self._decode_image(image_input)
        if img is None:
            return LivenessResult(
                is_real=False, score=0.0, label=-1,
                label_name='unknown', raw_scores=[],
                error='Could not decode image.',
            )

        # 2. Обнаружение лица
        bbox = self.detector.get_bbox(img)
        if bbox is None:
            return LivenessResult(
                is_real=False, score=0.0, label=-1,
                label_name='unknown', raw_scores=[],
                face_bbox=None,
                error='No face detected in image.',
            )

        # 3. Ансамбль всех моделей
        prediction = None

        for model_path, (model, _) in self.loaded_models.items():
            model_name = os.path.basename(model_path)
            h_input, w_input, _, scale = parse_model_name(model_name)

            crop_param = {
                'org_img': img,
                'bbox': bbox,
                'scale': scale,
                'out_w': w_input,
                'out_h': h_input,
                'crop': scale is not None,
            }
            patch = self.image_cropper.crop(**crop_param)
            probs = self._infer_one_model(model, patch)

            prediction = probs if prediction is None else prediction + probs

        prediction /= len(self.loaded_models)

        # 4. Интерпретация (класс 1 = живое лицо)
        label = int(np.argmax(prediction[0]))
        real_score = float(prediction[0][1]) if prediction.shape[1] > 1 else 0.0
        is_real = (label == 1) and (real_score >= threshold)

        return LivenessResult(
            is_real=is_real,
            score=real_score,
            label=label,
            label_name=LABEL_NAMES.get(label, 'unknown'),
            raw_scores=prediction[0].tolist(),
            face_bbox=bbox,
        )