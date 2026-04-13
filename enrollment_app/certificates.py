from __future__ import annotations

from pathlib import Path


def _escape_pdf_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_certificate_pdf(
    output_path: Path,
    certificate_number: str,
    client_name: str,
    course_title: str,
    issue_date: str,
    batch_code: str,
) -> None:
    lines = [
        "Maritime Training Certificate",
        f"Certificate No: {certificate_number}",
        "",
        "This certifies that",
        client_name,
        "successfully completed",
        course_title,
        f"Batch: {batch_code}",
        f"Issued: {issue_date}",
    ]
    text_commands = []
    y = 730
    for line in lines:
        text_commands.append(f"BT /F1 18 Tf 72 {y} Td ({_escape_pdf_text(line)}) Tj")
        y -= 34
    content_stream = "BT 0 0 Td " + " ET BT ".join(text_commands) + " ET"
    content_bytes = content_stream.encode("latin-1", errors="ignore")

    pdf = (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj\n"
        b"4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"
        + f"5 0 obj << /Length {len(content_bytes)} >> stream\n".encode("ascii")
        + content_bytes
        + b"\nendstream endobj\n"
    )

    xref_start = len(pdf)
    xref = (
        b"xref\n0 6\n"
        b"0000000000 65535 f \n"
        b"0000000009 00000 n \n"
        b"0000000058 00000 n \n"
        b"0000000115 00000 n \n"
        b"0000000241 00000 n \n"
        + f"{pdf.find(b'5 0 obj'):010d} 00000 n \n".encode("ascii")
    )
    trailer = (
        b"trailer << /Size 6 /Root 1 0 R >>\nstartxref\n"
        + str(xref_start).encode("ascii")
        + b"\n%%EOF"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(pdf + xref + trailer)

