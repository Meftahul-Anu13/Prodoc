"""Render one page of a stored original as PNG for the side-by-side review screen."""
import io
from pathlib import Path

MAX_SIDE = 1500
MAX_PIXELS = 40_000_000
IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.tif', '.tiff'}


def supported(filename):
    suffix = Path(filename).suffix.lower()
    return suffix == '.pdf' or suffix in IMAGE_SUFFIXES


def render(path, filename, page):
    """Return PNG bytes; raises ValueError for an unavailable page or unreadable file."""
    if page < 1:
        raise ValueError('page_out_of_range')
    if Path(filename).suffix.lower() == '.pdf':
        import fitz
        with fitz.open(path) as document:
            if page > document.page_count:
                raise ValueError('page_out_of_range')
            source = document[page - 1]
            zoom = min(2.0, MAX_SIDE / max(source.rect.width, source.rect.height))
            return source.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False).tobytes('png')
    from PIL import Image, ImageOps
    with Image.open(path) as image:
        if page > getattr(image, 'n_frames', 1):
            raise ValueError('page_out_of_range')
        image.seek(page - 1)
        if image.width * image.height > MAX_PIXELS:
            raise ValueError('image_too_large')
        with ImageOps.exif_transpose(image) as oriented:
            frame = oriented.convert('RGB')
        frame.thumbnail((MAX_SIDE, MAX_SIDE))
        output = io.BytesIO()
        frame.save(output, format='PNG')
        return output.getvalue()
