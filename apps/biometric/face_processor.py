"""
apps/biometric/face_processor.py

Процессор биометрических данных лица.
Архитектура: MTCNN (детекция) → InceptionResNetV1/VGGFace2 (эмбеддинг, 512d).
"""

import json
import logging
import base64
import io
import numpy as np
from PIL import Image

logger = logging.getLogger('apps.biometric.face_processor')

_processor_instance = None

def get_processor():
    global _processor_instance
    if _processor_instance is None:
        _processor_instance = FaceProcessor()
    return _processor_instance

class FaceProcessor:
    """
    Двухэтапный конвейер обработки лица:
        1. MTCNN — обнаружение, выравнивание, обрезка лица.
        2. InceptionResNetV1 (VGGFace2) — 512-мерный вектор признаков.
    """

    def __init__(self):
        self.device = None
        self.mtcnn  = None
        self.resnet = None
        self._initialized = False
        self._init_error  = None
        self._try_init()

    def _try_init(self):
        try:
            import torch
            from facenet_pytorch import MTCNN, InceptionResnetV1

            self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
            logger.info(f'FaceProcessor: устройство — {self.device}')

            self.mtcnn = MTCNN(
                image_size=160, margin=20, min_face_size=40,
                thresholds=[0.6, 0.7, 0.7], factor=0.709,
                keep_all=False, device=self.device,
            )
            self.resnet = InceptionResnetV1(pretrained='vggface2').eval().to(self.device)
            self._initialized = True
            logger.info('FaceProcessor: модели загружены успешно')

        except Exception as e:
            self._init_error = str(e)
            logger.error(f'FaceProcessor: ошибка инициализации — {e}')

    @property
    def is_ready(self):
        return self._initialized

    def decode_image(self, image_data: str) -> Image.Image:
        if ',' in image_data:
            image_data = image_data.split(',', 1)[1]
        img_bytes = base64.b64decode(image_data)
        return Image.open(io.BytesIO(img_bytes)).convert('RGB')

    def detect_face(self, img: Image.Image):
        """
        Возвращает (face_tensor, confidence, box) или (None, 0.0, None).
        face_tensor: 3×160×160 float tensor (нормализован).
        """
        if not self._initialized:
            return None, 0.0, None
        try:
            import torch
            boxes, probs = self.mtcnn.detect(img)
            if boxes is None or len(boxes) == 0:
                return None, 0.0, None

            confidence = float(probs[0]) if probs is not None else 0.0
            box = [float(x) for x in boxes[0]]

            face_tensor = self.mtcnn(img)
            if face_tensor is None:
                return None, confidence, box

            return face_tensor, confidence, box
        except Exception as e:
            logger.error(f'detect_face: {e}')
            return None, 0.0, None

    def get_embedding(self, face_tensor) -> np.ndarray | None:
        """512-мерный L2-нормализованный вектор признаков."""
        if not self._initialized:
            return None
        try:
            import torch
            with torch.no_grad():
                batch = face_tensor.unsqueeze(0).to(self.device)
                emb   = self.resnet(batch)
            return emb.cpu().numpy()[0]
        except Exception as e:
            logger.error(f'get_embedding: {e}')
            return None

    def cosine_similarity(self, emb1: np.ndarray, emb2: np.ndarray) -> float:
        n1, n2 = np.linalg.norm(emb1), np.linalg.norm(emb2)
        if n1 < 1e-9 or n2 < 1e-9:
            return 0.0
        return float(np.dot(emb1, emb2) / (n1 * n2))

    def embedding_to_json(self, embedding: np.ndarray) -> str:
        return json.dumps(embedding.tolist())

    def embedding_from_json(self, json_str: str) -> np.ndarray:
        return np.array(json.loads(json_str), dtype=np.float32)

    # Регистрация

    def process_registration_image(self, image_data: str) -> dict:
        """
        Полный пайплайн регистрации.

        Returns:
            success               bool
            embedding             str (JSON)
            detection_confidence  float
            quality_score         float
            error                 str | None
        """
        from apps.biometric.quality import calculate_quality
        try:
            img     = self.decode_image(image_data)
            quality = calculate_quality(img)

            face_tensor, det_conf, box = self.detect_face(img)
            if face_tensor is None:
                return {
                    'success': False,
                    'error': 'Лицо не обнаружено на изображении',
                    'quality_score': quality, 'detection_confidence': 0.0,
                }

            embedding = self.get_embedding(face_tensor)
            if embedding is None:
                return {
                    'success': False,
                    'error': 'Ошибка извлечения биометрических признаков',
                    'quality_score': quality, 'detection_confidence': det_conf,
                }

            return {
                'success': True,
                'embedding': self.embedding_to_json(embedding),
                'detection_confidence': det_conf,
                'quality_score': quality,
                'box': box,
                'error': None,
            }
        except Exception as e:
            logger.error(f'process_registration_image: {e}')
            return {
                'success': False,
                'error': f'Внутренняя ошибка: {e}',
                'quality_score': 0.0, 'detection_confidence': 0.0,
            }

    # Аутентификация

    def process_authentication_image(
        self,
        image_data: str,
        stored_embedding_json: str,
        threshold: float = 0.65,
    ) -> dict:
        """
        Полный пайплайн аутентификации.

        Returns (помимо стандартных полей):
            current_embedding np.ndarray | None
                Вектор признаков ТЕКУЩЕГО кадра. Используется liveness-модулем
                как эталон для same-person verification во время активного
                challenge. Позволяет гарантировать, что challenge проходит тот
                же человек, который прошёл распознавание.
        """
        from apps.biometric.quality import calculate_quality
        try:
            img     = self.decode_image(image_data)
            quality = calculate_quality(img)

            face_tensor, det_conf, box = self.detect_face(img)
            if face_tensor is None:
                return {
                    'success': False,
                    'recognition_confidence': 0.0,
                    'quality_score': quality,
                    'liveness_score': None,
                    'result_code': 'fail_no_face',
                    'error': 'Лицо не обнаружено',
                    'current_embedding': None,
                }

            embedding = self.get_embedding(face_tensor)
            if embedding is None:
                return {
                    'success': False,
                    'recognition_confidence': 0.0,
                    'quality_score': quality,
                    'liveness_score': None,
                    'result_code': 'fail_no_face',
                    'error': 'Ошибка обработки лица',
                    'current_embedding': None,
                }

            stored_emb = self.embedding_from_json(stored_embedding_json)
            similarity = self.cosine_similarity(embedding, stored_emb)

            liveness_score = None  # заполняется liveness-модулем снаружи

            base = {
                'quality_score':         quality,
                'liveness_score':        liveness_score,
                'current_embedding':     embedding,
            }

            if similarity >= threshold:
                return {
                    **base,
                    'success': True,
                    'recognition_confidence': similarity,
                    'result_code': 'success',
                    'error': None,
                }
            else:
                return {
                    **base,
                    'success': False,
                    'recognition_confidence': similarity,
                    'result_code': 'fail_recognition',
                    'error': f'Лицо не совпадает (схожесть: {similarity:.2%})',
                }

        except Exception as e:
            logger.error(f'process_authentication_image: {e}')
            return {
                'success': False,
                'recognition_confidence': 0.0,
                'quality_score': 0.0,
                'liveness_score': None,
                'result_code': 'fail_no_face',
                'error': f'Внутренняя ошибка: {e}',
                'current_embedding': None,
            }
