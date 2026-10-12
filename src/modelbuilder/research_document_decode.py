"""Standalone, offline Poppler/Tesseract extraction with Linux hard limits.

Only bytes and fixed extraction options enter this worker. No caller pathname,
URL, password, shell, plugin, or inference provider is accepted. The parent owns
its process group and deadline. Resource limits are not a hostile-code sandbox.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile

MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_TEXT_BYTES = 200_000
MAX_PAGES = 100
MAX_OCR_PAGES = 20
MAX_OUTPUT_BYTES = 1_400_000
TOOLS = {name: f"/usr/bin/{name}" for name in ("pdfinfo", "pdftotext", "pdftoppm", "tesseract")}


class DecodeError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def run_tool(arguments, *, limit=MAX_OUTPUT_BYTES):
    # Files bound child output before allocating any parent-side bytes. No
    # stderr diagnostics (which can contain source text) cross this boundary.
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(arguments, stdin=subprocess.DEVNULL, stdout=output,
                                    stderr=subprocess.DEVNULL, check=False, timeout=85)
        except subprocess.TimeoutExpired:
            raise DecodeError("document_timeout") from None
        except OSError:
            raise DecodeError("document_capability_unavailable") from None
        size = os.fstat(output.fileno()).st_size
        if size > limit or result.returncode < 0:
            raise DecodeError("document_limit_exceeded")
        if result.returncode:
            raise DecodeError("invalid_document")
        output.seek(0)
        return output.read(limit + 1)


def text_value(raw):
    try:
        value = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").strip()
    except UnicodeError:
        raise DecodeError("invalid_document") from None
    if "\x00" in value or any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise DecodeError("invalid_document")
    return value


def decode(data, options, directory):
    if not data.startswith(b"%PDF-"):
        raise DecodeError("invalid_document")
    required = ["pdfinfo", "pdftotext"]
    if options["ocr_mode"] != "disabled":
        required += ["pdftoppm", "tesseract"]
    if any(not os.access(TOOLS[name], os.X_OK) for name in required):
        raise DecodeError("document_capability_unavailable")
    versions = {}
    for name in required:
        # Poppler writes its version to stderr; query through a separate,
        # bounded file, never merge document diagnostics into the response.
        with tempfile.TemporaryFile() as version_output:
            command = [TOOLS[name], "--version" if name == "tesseract" else "-v"]
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=version_output,
                                    stderr=version_output, check=False, timeout=5)
            version_output.seek(0)
            raw_version = version_output.read(8193)
        if result.returncode or len(raw_version) > 8192:
            raise DecodeError("document_capability_unavailable")
        version = raw_version.decode("utf-8", errors="replace").splitlines()[0]
        if not version.startswith(name + " ") or len(version) > 200:
            raise DecodeError("document_capability_unavailable")
        versions[name] = version
    if options["ocr_mode"] != "disabled":
        languages = run_tool([TOOLS["tesseract"], "--list-langs"], limit=64000).decode("utf-8", errors="replace")
        if "eng" not in languages.splitlines():
            raise DecodeError("document_capability_unavailable")
    source = directory / "source.pdf"
    source.write_bytes(data)
    information = run_tool([TOOLS["pdfinfo"], str(source)], limit=64000).decode("utf-8", errors="replace")
    # Never request passwords or override PDF copy/access restrictions.
    if not re.search(r"^Encrypted:\s+no\s*$", information, re.M):
        raise DecodeError("document_access_restricted")
    matched = re.search(r"^Pages:\s+([0-9]+)\s*$", information, re.M)
    if matched is None:
        raise DecodeError("invalid_document")
    count = int(matched[1])
    if not 1 <= count <= options["max_pages"]:
        raise DecodeError("document_limit_exceeded")
    raw_text = run_tool([TOOLS["pdftotext"], "-enc", "UTF-8", "-eol", "unix", "-layout", str(source), "-"],
                        limit=MAX_TEXT_BYTES + 4 * MAX_PAGES)
    pages = raw_text.split(b"\x0c")
    if len(pages) != count + 1 or pages[-1].strip():
        raise DecodeError("invalid_document")
    extracted = [text_value(value) for value in pages[:-1]]
    ocr_pages = [index for index, value in enumerate(extracted)
                 if options["ocr_mode"] == "always" or (options["ocr_mode"] == "auto" and not value)]
    if len(ocr_pages) > options["max_ocr_pages"]:
        raise DecodeError("document_limit_exceeded")
    for index in ocr_pages:
        prefix = directory / "page"
        image = directory / "page.png"
        try:
            run_tool([TOOLS["pdftoppm"], "-f", str(index + 1), "-l", str(index + 1), "-r", "150",
                      "-scale-to", "2048", "-gray", "-png", "-singlefile", str(source), str(prefix)], limit=1024)
            if not image.is_file() or not 0 < image.stat().st_size <= 16 * 1024 * 1024:
                raise DecodeError("document_limit_exceeded")
            extracted[index] = text_value(run_tool([TOOLS["tesseract"], str(image), "stdout", "-l", "eng",
                                                   "--psm", "3"], limit=MAX_TEXT_BYTES))
        finally:
            image.unlink(missing_ok=True)
    records, parts, offset, total = [], [], 0, 0
    for index, value in enumerate(extracted):
        encoded = value.encode("utf-8")
        total += len(encoded) + (2 if index else 0)
        if total > MAX_TEXT_BYTES:
            raise DecodeError("document_limit_exceeded")
        if index:
            parts.append("\n\n")
            offset += 2
        method = "local_ocr" if index in ocr_pages else "embedded_text"
        records.append({"page_number": index + 1, "start": offset, "end": offset + len(value),
                        "sha256": hashlib.sha256(encoded).hexdigest(), "byte_length": len(encoded),
                        "extraction_method": method, "empty": not bool(value)})
        parts.append(value)
        offset += len(value)
    text = "".join(parts)
    if not text.strip():
        raise DecodeError("document_no_text")
    return {"text": text, "pages": records, "tools": versions}


def main():
    if sys.platform != "linux":
        raise DecodeError("document_capability_unavailable")
    def interrupted(_signal, _frame):
        # subprocess.run kills and waits for its direct child when unwinding
        # an exception. Let the parent stop the group without orphaning tools.
        raise DecodeError("document_timeout")
    signal.signal(signal.SIGTERM, interrupted)
    try:
        import resource
        for kind, limits in [(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2),
                             (resource.RLIMIT_CPU, (60, 61)),
                             (resource.RLIMIT_FSIZE, (16 * 1024 * 1024,) * 2),
                             (resource.RLIMIT_NOFILE, (64, 64)),
                             (resource.RLIMIT_CORE, (0, 0))]:
            resource.setrlimit(kind, limits)
    except (ImportError, OSError, ValueError):
        raise DecodeError("document_capability_unavailable") from None
    options = json.loads(sys.stdin.buffer.readline(4096))
    if (not isinstance(options, dict) or set(options) != {"ocr_mode", "max_pages", "max_ocr_pages"}
            or options["ocr_mode"] not in {"disabled", "auto", "always"}
            or type(options["max_pages"]) is not int or not 1 <= options["max_pages"] <= MAX_PAGES
            or type(options["max_ocr_pages"]) is not int or not 1 <= options["max_ocr_pages"] <= MAX_OCR_PAGES):
        raise DecodeError("invalid_document")
    data = sys.stdin.buffer.read(MAX_DOCUMENT_BYTES + 1)
    if len(data) > MAX_DOCUMENT_BYTES:
        raise DecodeError("document_limit_exceeded")
    # The parent owns this private directory and removes it even if it kills us.
    directory = Path.cwd()
    result = decode(data, options, directory)
    wire = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(wire) > MAX_OUTPUT_BYTES:
        raise DecodeError("document_limit_exceeded")
    sys.stdout.buffer.write(wire)
    return 0


if __name__ == "__main__":
    try:
        status = main()
    except DecodeError as error:
        sys.stdout.buffer.write(json.dumps({"error": error.code}).encode())
        status = 1
    except (MemoryError, OverflowError):
        sys.stdout.buffer.write(b'{"error":"document_limit_exceeded"}')
        status = 1
    except Exception:
        sys.stdout.buffer.write(b'{"error":"invalid_document"}')
        status = 1
    raise SystemExit(status)
