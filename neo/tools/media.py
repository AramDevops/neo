from __future__ import annotations

import json
import re
import struct
import zlib
from pathlib import Path
from typing import Any

from .base import ToolResult, ToolboxHelpers


class MediaTools(ToolboxHelpers):
    """Bounded metadata and text extraction for images and PDFs."""

    def _image_info(self, path: str) -> ToolResult:
        target = self._safe_path(path)
        if not target.exists() or not target.is_file():
            return ToolResult(False, "image_info", f"File not found: {path}", {})
        stat = target.stat()
        header = target.read_bytes()[:65536]
        payload = {
            "path": str(target.relative_to(self.workspace)),
            "size_bytes": stat.st_size,
            "format": "unknown",
            "width": None,
            "height": None,
            "details": {},
        }
        detected = self._detect_image_info(target, header)
        payload.update(detected)
        ok = payload["format"] != "unknown"
        output = json.dumps(payload, indent=2)
        if not ok:
            output = "Unsupported or unrecognized image format.\n" + output
        return ToolResult(ok, "image_info", output, payload)

    def _detect_image_info(self, target: Path, data: bytes) -> dict:
        if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 29:
            width, height = struct.unpack(">II", data[16:24])
            return {
                "format": "png",
                "width": width,
                "height": height,
                "details": {"bit_depth": data[24], "color_type": data[25]},
            }
        if data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
            width, height = struct.unpack("<HH", data[6:10])
            return {"format": "gif", "width": width, "height": height, "details": {"version": data[:6].decode("ascii", errors="replace")}}
        if data.startswith(b"\xff\xd8"):
            jpeg = self._jpeg_dimensions(data)
            if jpeg:
                return {"format": "jpeg", **jpeg}
        if data.startswith(b"BM") and len(data) >= 26:
            width = int.from_bytes(data[18:22], "little", signed=True)
            height = abs(int.from_bytes(data[22:26], "little", signed=True))
            return {"format": "bmp", "width": width, "height": height, "details": {}}
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            webp = self._webp_dimensions(data)
            if webp:
                return {"format": "webp", **webp}
        if target.suffix.lower() == ".svg":
            return self._svg_dimensions(target)
        return {}

    def _jpeg_dimensions(self, data: bytes) -> dict | None:
        i = 2
        sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            while i < len(data) and data[i] == 0xFF:
                i += 1
            if i >= len(data):
                break
            marker = data[i]
            i += 1
            if marker in {0xD8, 0xD9}:
                continue
            if i + 2 > len(data):
                break
            segment_length = int.from_bytes(data[i:i + 2], "big")
            if segment_length < 2 or i + segment_length > len(data):
                break
            if marker in sof_markers and segment_length >= 7:
                precision = data[i + 2]
                height = int.from_bytes(data[i + 3:i + 5], "big")
                width = int.from_bytes(data[i + 5:i + 7], "big")
                return {"width": width, "height": height, "details": {"precision": precision, "sof_marker": hex(marker)}}
            i += segment_length
        return None

    def _webp_dimensions(self, data: bytes) -> dict | None:
        chunk = data[12:16]
        if chunk == b"VP8X" and len(data) >= 30:
            width = int.from_bytes(data[24:27], "little") + 1
            height = int.from_bytes(data[27:30], "little") + 1
            return {"width": width, "height": height, "details": {"variant": "VP8X"}}
        if chunk == b"VP8L" and len(data) >= 25:
            b0, b1, b2, b3 = data[21], data[22], data[23], data[24]
            width = 1 + (((b1 & 0x3F) << 8) | b0)
            height = 1 + (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6))
            return {"width": width, "height": height, "details": {"variant": "VP8L"}}
        if chunk == b"VP8 " and len(data) >= 30:
            width = int.from_bytes(data[26:28], "little") & 0x3FFF
            height = int.from_bytes(data[28:30], "little") & 0x3FFF
            return {"width": width, "height": height, "details": {"variant": "VP8"}}
        return None

    def _svg_dimensions(self, target: Path) -> dict:
        text = target.read_text(encoding="utf-8", errors="replace")[:200000]
        width = self._svg_number_attr(text, "width")
        height = self._svg_number_attr(text, "height")
        viewbox = re.search(r"viewBox\s*=\s*['\"]\s*([0-9.+-]+)\s+([0-9.+-]+)\s+([0-9.+-]+)\s+([0-9.+-]+)", text)
        details: dict[str, Any] = {}
        if viewbox:
            details["viewBox"] = " ".join(viewbox.groups())
            width = width or float(viewbox.group(3))
            height = height or float(viewbox.group(4))
        return {"format": "svg", "width": width, "height": height, "details": details}

    def _svg_number_attr(self, text: str, name: str) -> float | None:
        match = re.search(rf"\b{name}\s*=\s*['\"]\s*([0-9.]+)", text, re.I)
        if not match:
            return None
        try:
            value = float(match.group(1))
        except ValueError:
            return None
        return int(value) if value.is_integer() else value

    def _pdf_info(self, path: str) -> ToolResult:
        target = self._safe_path(path)
        if not target.exists() or not target.is_file():
            return ToolResult(False, "pdf_info", f"File not found: {path}", {})
        data = self._read_limited_bytes(target, 10_000_000)
        header = re.match(rb"%PDF-(\d\.\d)", data[:20])
        if not header:
            return ToolResult(False, "pdf_info", "File does not look like a PDF.", {"path": str(target)})
        payload = {
            "path": str(target.relative_to(self.workspace)),
            "size_bytes": target.stat().st_size,
            "pdf_version": header.group(1).decode("ascii", errors="replace"),
            "page_count_estimate": len(re.findall(rb"/Type\s*/Page\b", data)),
            "encrypted": b"/Encrypt" in data,
            "linearized": b"/Linearized" in data[:2048],
            "title": self._pdf_metadata_string(data, "Title"),
            "author": self._pdf_metadata_string(data, "Author"),
            "truncated": target.stat().st_size > len(data),
        }
        return ToolResult(True, "pdf_info", json.dumps(payload, indent=2), payload)

    def _pdf_text_extract(self, path: str, page_limit: int, char_limit: int) -> ToolResult:
        target = self._safe_path(path)
        if not target.exists() or not target.is_file():
            return ToolResult(False, "pdf_text_extract", f"File not found: {path}", {})
        page_limit = max(1, min(page_limit, 20))
        char_limit = max(500, min(char_limit, 50000))
        data = self._read_limited_bytes(target, 12_000_000)
        if not data.startswith(b"%PDF-"):
            return ToolResult(False, "pdf_text_extract", "File does not look like a PDF.", {"path": str(target)})

        optional_note = ""
        text = ""
        extractor = ""
        try:
            try:
                from pypdf import PdfReader  # type: ignore
                extractor = "pypdf"
            except ImportError:
                from PyPDF2 import PdfReader  # type: ignore
                extractor = "PyPDF2"
            reader = PdfReader(str(target))
            parts = []
            for page in list(reader.pages)[:page_limit]:
                parts.append(page.extract_text() or "")
            text = "\n\n".join(parts)
        except ImportError:
            optional_note = "pypdf/PyPDF2 not installed; used built-in best-effort extractor."
        except Exception as exc:
            optional_note = f"optional PDF extractor failed ({type(exc).__name__}); used built-in best-effort extractor."

        if not text:
            extractor = "builtin_best_effort"
            text = self._extract_pdf_text_builtin(data)
        text = self._redact_sensitive(re.sub(r"[ \t]{2,}", " ", text).strip())[:char_limit]
        payload = {
            "path": str(target.relative_to(self.workspace)),
            "extractor": extractor or "builtin_best_effort",
            "page_limit": page_limit,
            "char_limit": char_limit,
            "text": text,
            "truncated": len(text) >= char_limit,
            "optional_dependency": optional_note,
        }
        return ToolResult(True, "pdf_text_extract", json.dumps(payload, indent=2, ensure_ascii=False), payload)

    def _pdf_metadata_string(self, data: bytes, key: str) -> str:
        pattern = rb"/" + re.escape(key.encode("ascii")) + rb"\s*(\((?:\\.|[^\\)])*\)|<[^>]+>)"
        match = re.search(pattern, data[:2_000_000], re.S)
        if not match:
            return ""
        return self._decode_pdf_string(match.group(1))[:500]

    def _extract_pdf_text_builtin(self, data: bytes) -> str:
        parts: list[str] = []
        for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
            header = data[max(0, match.start() - 600):match.start()]
            stream = match.group(1).strip(b"\r\n")
            if b"FlateDecode" in header:
                try:
                    stream = zlib.decompress(stream)
                except Exception:
                    continue
            parts.extend(self._pdf_text_strings(stream))
            if sum(len(part) for part in parts) > 60000:
                break
        if not parts:
            parts = [self._decode_pdf_string(match.group(0)) for match in re.finditer(rb"\((?:\\.|[^\\)])*\)", data[:2_000_000], re.S)]
        text = " ".join(part for part in parts if part)
        return re.sub(r"\s+", " ", text).strip()

    def _pdf_text_strings(self, stream: bytes) -> list[str]:
        strings = []
        for match in re.finditer(rb"\((?:\\.|[^\\)])*\)", stream, re.S):
            decoded = self._decode_pdf_string(match.group(0))
            if decoded and self._looks_like_human_text(decoded):
                strings.append(decoded)
        for match in re.finditer(rb"<([0-9A-Fa-f\s]{4,})>\s*(?:Tj|'|\"|TJ)", stream):
            try:
                raw = bytes.fromhex(re.sub(rb"\s+", b"", match.group(1)).decode("ascii"))
            except Exception:
                continue
            decoded = raw.decode("utf-16-be" if raw.startswith(b"\xfe\xff") else "utf-8", errors="replace")
            if decoded and self._looks_like_human_text(decoded):
                strings.append(decoded)
        return strings

    def _decode_pdf_string(self, raw: bytes) -> str:
        raw = raw.strip()
        if raw.startswith(b"<") and raw.endswith(b">") and not raw.startswith(b"<<"):
            try:
                data = bytes.fromhex(re.sub(rb"\s+", b"", raw[1:-1]).decode("ascii"))
                return data.decode("utf-16-be" if data.startswith(b"\xfe\xff") else "utf-8", errors="replace").strip("﻿")
            except Exception:
                return ""
        if not (raw.startswith(b"(") and raw.endswith(b")")):
            return raw.decode("utf-8", errors="replace")
        body = raw[1:-1]
        out = bytearray()
        i = 0
        escapes = {
            ord("n"): ord("\n"),
            ord("r"): ord("\r"),
            ord("t"): ord("\t"),
            ord("b"): ord("\b"),
            ord("f"): ord("\f"),
            ord("("): ord("("),
            ord(")"): ord(")"),
            ord("\\"): ord("\\"),
        }
        while i < len(body):
            char = body[i]
            if char != 0x5C:
                out.append(char)
                i += 1
                continue
            i += 1
            if i >= len(body):
                break
            nxt = body[i]
            if nxt in b"\r\n":
                while i < len(body) and body[i] in b"\r\n":
                    i += 1
                continue
            if 48 <= nxt <= 55:
                digits = bytes([nxt])
                i += 1
                for _ in range(2):
                    if i < len(body) and 48 <= body[i] <= 55:
                        digits += bytes([body[i]])
                        i += 1
                    else:
                        break
                out.append(int(digits, 8) & 0xFF)
                continue
            out.append(escapes.get(nxt, nxt))
            i += 1
        data = bytes(out)
        encoding = "utf-16-be" if data.startswith(b"\xfe\xff") else "utf-8"
        return data.decode(encoding, errors="replace").strip("﻿")
