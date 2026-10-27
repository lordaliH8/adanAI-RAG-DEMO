"""Shared Persian normalization; raw source files are never modified."""
from __future__ import annotations

import re
import unicodedata

DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
LETTERS = str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک", "ۀ": "ه", "ة": "ه"})


def normalize_text(value: str, *, indexing: bool = False) -> str:
    text = unicodedata.normalize("NFKC", str(value)).translate(DIGITS).translate(LETTERS)
    text = "".join(char for char in text if unicodedata.category(char) != "Cf" or char == "\u200c")
    text = text.replace("\u200c", " ")
    text = "".join(c for c in text if not unicodedata.category(c).startswith("M"))
    if indexing:
        # PDF glyph positions can join an LTR code/date directly to RTL prose.
        arabic_letters = "ء-يپچژگکیۀ"
        text = re.sub(r"(?<=[A-Za-z0-9])(?=[" + arabic_letters + r"])", " ", text)
        text = re.sub(r"(?<=[" + arabic_letters + r"])(?=[A-Za-z0-9])", " ", text)
        # The authored PDFs' lam-alef ligature can map visually in this form.
        # Kept explicit and narrow; never apply a blanket character reversal.
        repairs = {"کاال": "کالا", "طالیی": "طلایی", "اطالعات": "اطلاعات",
                   "اختالف": "اختلاف", "اقالم": "اقلام", "عالوه": "علاوه",
                   "اعالم": "اعلام", "اصالح": "اصلاح", "باال": "بالا", "الزم": "لازم"}
        for malformed, corrected in repairs.items():
            text = re.sub(r"\b" + malformed, corrected, text)
    return re.sub(r"\s+", " ", text).strip()


def lexical_tokens(text: str) -> list[str]:
    return re.findall(r"[a-z]+(?:[-_.][a-z0-9]+)*|\d+(?:[-./]\d+)*|[\u0621-\u06ff]+",
                      normalize_text(text, indexing=True).lower())
