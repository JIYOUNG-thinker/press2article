"""
hwp_reader.py — 한글(HWP/HWPX) 파일에서 본문 텍스트를 뽑아내는 모듈

Upstage 에이전트는 HWP를 직접 읽지 못하므로, 서버에서 먼저 텍스트를 꺼낸 뒤
Word 문서로 바꿔 에이전트에 넘긴다.

- HWP (한글 2002~ 이진 형식): OLE 컨테이너 안의 BodyText/Section* 스트림을 읽어
  '문단 텍스트' 레코드(HWPTAG_PARA_TEXT)만 모은다. 표 안의 글자도 포함된다.
- HWPX (한글 2014~ 개방형 형식): ZIP 안의 Contents/section*.xml에서 글자(<hp:t>)를 모은다.
"""
import io
import re
import struct
import zipfile
import zlib
import xml.etree.ElementTree as ET

import olefile

HWPTAG_PARA_TEXT = 67  # HWPTAG_BEGIN(16) + 51

# 문단 텍스트 안의 제어 문자 중 1글자(2바이트)짜리 — 나머지 제어 문자는 8글자(16바이트)를 차지
_CHAR_CONTROLS = {0, 10, 13, 24, 25, 26, 27, 28, 29, 30, 31}


class HwpError(ValueError):
    pass


def _para_text(rec: bytes) -> str:
    out, i, n = [], 0, len(rec) // 2
    while i < n:
        c = struct.unpack_from("<H", rec, i * 2)[0]
        if c >= 32:
            out.append(chr(c))
            i += 1
        elif c in (10, 13):
            out.append("\n")
            i += 1
        elif c == 9:  # 탭(인라인 제어 문자, 8글자 크기)
            out.append(" ")
            i += 8
        elif c in _CHAR_CONTROLS:
            i += 1
        else:  # 표·그림·각주 등 개체를 가리키는 제어 문자 → 건너뜀
            i += 8
    return "".join(out).strip()


def hwp_to_text(data: bytes) -> str:
    if not olefile.isOleFile(io.BytesIO(data)):
        raise HwpError("HWP 파일 형식이 아니에요. 파일이 손상됐을 수 있어요.")
    ole = olefile.OleFileIO(io.BytesIO(data))
    try:
        header = ole.openstream("FileHeader").read()
        flags = struct.unpack_from("<I", header, 36)[0]
        compressed, encrypted, distribution = flags & 1, flags & 2, flags & 4
        if encrypted:
            raise HwpError("암호가 걸린 한글 파일이에요. 암호를 풀고 다시 올려주세요.")
        if distribution:
            raise HwpError("배포용 한글 문서는 읽을 수 없어요. 한글에서 'PDF로 저장'한 뒤 올려주세요.")

        sections = [d for d in ole.listdir() if len(d) == 2 and d[0] == "BodyText"]
        sections.sort(key=lambda d: int(re.sub(r"\D", "", d[1]) or 0))
        paras = []
        for sec in sections:
            raw = ole.openstream(sec).read()
            if compressed:
                raw = zlib.decompress(raw, -15)
            i = 0
            while i + 4 <= len(raw):
                head = struct.unpack_from("<I", raw, i)[0]
                tag, size = head & 0x3FF, (head >> 20) & 0xFFF
                i += 4
                if size == 0xFFF:
                    size = struct.unpack_from("<I", raw, i)[0]
                    i += 4
                if tag == HWPTAG_PARA_TEXT:
                    t = _para_text(raw[i:i + size])
                    if t:
                        paras.append(t)
                i += size
    finally:
        ole.close()
    return "\n".join(paras).strip()


def hwpx_to_text(data: bytes) -> str:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HwpError("HWPX 파일 형식이 아니에요. 파일이 손상됐을 수 있어요.")
    names = [n for n in z.namelist() if re.match(r"Contents/section\d+\.xml$", n)]
    names.sort(key=lambda n: int(re.sub(r"\D", "", n)))
    lines = []
    for name in names:
        root = ET.fromstring(z.read(name))
        cur = []
        for el in root.iter():
            tag = el.tag.rsplit("}", 1)[-1]
            if tag == "p" and cur:  # 새 문단 시작
                lines.append("".join(cur).strip())
                cur = []
            elif tag == "t":
                cur.append(el.text or "")
                for child in el:  # <hp:t> 안의 탭 등 하위 요소 뒤 글자
                    cur.append(child.tail or "")
        if cur:
            lines.append("".join(cur).strip())
    return "\n".join(l for l in lines if l).strip()


def hangul_to_text(filename: str, data: bytes) -> str:
    ext = filename.rsplit(".", 1)[-1].lower()
    text = hwpx_to_text(data) if ext == "hwpx" else hwp_to_text(data)
    if not text:
        raise HwpError("한글 파일에서 글자를 찾지 못했어요. 이미지로만 된 문서라면 PDF로 저장해서 올려주세요.")
    return text
