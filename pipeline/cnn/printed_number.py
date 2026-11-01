"""PaddleOCR印刷题号读取；显式试用入口，不改默认数字CNN。

使用官方PP-OCRv6 medium ONNX权重与官方TextRecognition前后处理。
read(img)兼容DigitReader，read_details保留原文本，无法严格解析时返回0。
"""
import math
import re
import unicodedata


def parse_number(text):
    text = ''.join(unicodedata.normalize('NFKC', text).split())
    match = re.fullmatch(r'(?:\((\d{1,2})\)|(\d{1,2}))[.、。:：]?', text)
    if match is None:
        return 0
    number = int(match.group(1) or match.group(2))
    return number if 1 <= number <= 60 else 0


class PaddleNumberReader:
    def __init__(self, model_dir, cpu_threads=4):
        # Windows: preload project torch DLLs before Paddle/ModelScope imports.
        import torch  # noqa: F401
        from paddleocr import TextRecognition
        self.model = TextRecognition(
            model_name='PP-OCRv6_medium_rec', model_dir=str(model_dir),
            engine='onnxruntime', device='cpu', cpu_threads=cpu_threads,
        )

    def read_details(self, images):
        if not images:
            return []
        results = self.model.predict(input=images, batch_size=min(32, len(images)))
        if len(results) != len(images):
            raise RuntimeError('PaddleOCR返回数量与题号输入不一致')
        output = []
        for result in results:
            raw = str(result['rec_text'])
            confidence = float(result['rec_score'])
            if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise ValueError('PaddleOCR题号置信度非法')
            number = parse_number(raw)
            output.append({'number': number, 'confidence': confidence if number else 0.,
                           'raw_text': raw, 'raw_confidence': confidence})
        return output

    def read(self, image):
        if image is None or image.size == 0:
            return 0, 0.
        result = self.read_details([image])[0]
        return result['number'], result['confidence']
