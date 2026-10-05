"""Образцы файлов для тестов: строятся в памяти, без файлов в репозитории."""

import io

import docx
from PIL import Image

TEXT_LAYER = "This page has a real text layer inside"


def png(width: int = 40, height: int = 30, color: str = "white") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, "PNG")
    return buffer.getvalue()


def jpeg(width: int = 40, height: int = 30) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, "JPEG")
    return buffer.getvalue()


def scan_pdf(pages: int) -> bytes:
    """PDF без текстового слоя: каждая страница — картинка."""
    images = [Image.new("RGB", (120, 160), "white") for _ in range(pages)]
    buffer = io.BytesIO()
    images[0].save(buffer, "PDF", save_all=True, append_images=images[1:])
    return buffer.getvalue()


def text_pdf(pages: list[str], page_size: tuple[int, int] = (612, 792)) -> bytes:
    """PDF с текстовым слоем; пустая строка — страница без текста (скан)."""
    width, height = page_size
    kids = " ".join(f"{4 + 2 * index} 0 R" for index in range(len(pages)))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for index, text in enumerate(pages):
        stream = f"BT /F1 12 Tf 72 {height - 72} Td ({text}) Tj ET".encode() if text else b""
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
                f"/Contents {5 + 2 * index} 0 R /Resources << /Font << /F1 3 0 R >> >> >>"
            ).encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    )
    return out.getvalue()


def docx_file(*paragraphs: str) -> bytes:
    document = docx.Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()
