"""Local parser installation smoke check using fictional documents, never an LLM."""
import json
import os
from pathlib import Path
from uuid import uuid4


def main():
    root = Path(__file__).resolve().parents[1]
    os.environ['ALLOW_CLOUD_EXTRACTION'] = 'false'
    os.environ.setdefault('MINERU_HOME', str(root / 'var' / 'mineru'))
    os.environ.setdefault('MINERU_TIER', 'basic')
    os.environ.setdefault('MINERU_MODEL_SOURCE', 'local')
    os.environ.setdefault('MINERU_MODEL_BASE_DIR', str(root / 'var' / 'mineru' / 'models'))
    os.environ.setdefault('MINERU_MODEL_SMALL_BACKEND', 'onnx')
    from backend.app.sources import read_sources
    import fitz

    output = root / 'var' / ('mineru-smoke-' + uuid4().hex)
    output.mkdir(parents=True)
    pdf = output / 'fictional-invoice.pdf'
    image = output / 'fictional-invoice.png'
    with fitz.open() as document:
        page = document.new_page(width=595, height=842)
        lines = ['FICTIONAL TEST INVOICE', 'Invoice number: LOCAL-SMOKE-001',
                 'Supplier: Fictional Seller', 'Customer: Fictional Buyer',
                 'Invoice date: 2026-10-29', 'Currency: USD',
                 'Description       Quantity       Unit price       Net amount',
                 'Synthetic service     2              50.00             100.00',
                 'Subtotal: 100.00', 'Tax: 10.00', 'Discount: 0.00',
                 'Other charges: 0.00', 'Total: 110.00']
        for index, text in enumerate(lines):
            page.insert_text((45, 60 + index * 29), text, fontsize=12)
        document.save(pdf)
        page.get_pixmap(matrix=fitz.Matrix(2, 2)).save(image)
    reports = []
    for source in (pdf, image):
        bundle = read_sources(source, source.name, output / (source.name + '.artifacts'))
        text = '\n'.join(block['text'] for block in bundle['blocks'])
        if len(bundle['pages']) != 1 or not text.strip() or bundle['metadata'].get('finalization_blocked'):
            raise RuntimeError('Local parser did not recover a complete nonempty synthetic page')
        reports.append({'filename': source.name, 'pages': len(bundle['pages']),
                        'blocks': len(bundle['blocks']), 'reader': bundle['metadata']['reader'],
                        'text_preview': text[:1200], 'warnings': bundle['warnings']})
    print(json.dumps({'local_parser_smoke': 'passed', 'cloud_calls': 0,
                      'artifact_directory': str(output), 'results': reports}, indent=2))


if __name__ == '__main__':
    main()
