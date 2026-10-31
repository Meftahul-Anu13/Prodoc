"""Setup-only model downloads; never sends documents or invokes a paid provider."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--embeddings', action='store_true', help='Also download BGE-small embedding weights')
    args = parser.parse_args()
    import rapidocr
    from rapidocr import RapidOCR, LangRec, ModelType, OCRVersion
    engine = RapidOCR(params={'Global.use_cls': False, 'Det.ocr_version': OCRVersion.PPOCRV4,
        'Det.model_type': ModelType.MOBILE, 'Rec.ocr_version': OCRVersion.PPOCRV4,
        'Rec.model_type': ModelType.MOBILE, 'Rec.lang_type': LangRec.EN,
        'EngineConfig.onnxruntime.intra_op_num_threads': 2, 'EngineConfig.onnxruntime.inter_op_num_threads': 1})
    # Recognition is lazy: a blank image would provision only the detector.
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new('RGB', (800, 160), 'white')
    ImageDraw.Draw(image).text((25, 45), 'Invoice INV-100 Total USD 110.00', fill='black', font=ImageFont.load_default(size=32))
    engine(image)
    directory = Path(rapidocr.__file__).parent / 'models'
    weights = {}
    for name in ('ch_PP-OCRv4_det_mobile.onnx', 'en_PP-OCRv4_rec_mobile.onnx'):
        path = directory / name
        if not path.is_file():
            raise RuntimeError(f'Model setup failed: {name}')
        weights[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if args.embeddings:
        from backend.app.search import embedding_model
        next(embedding_model().embed(['Model initialization check']))
    Path('var').mkdir(exist_ok=True)
    Path('var/model-manifest.json').write_text(json.dumps(weights, indent=2), encoding='utf-8')
    print('OCR models ready; checksums: var/model-manifest.json')


if __name__ == '__main__':
    main()
