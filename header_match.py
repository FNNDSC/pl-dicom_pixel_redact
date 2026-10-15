"""Match text found in an image against values from selected DICOM header tags.

Used by ``--propagate-match-tags``: only redaction boxes whose OCR'd text
matches the header values of the chosen tags (``PatientID``, ``PatientName``,
``PatientBirthDate``, ``AccessionNumber``, ``InstitutionName``, ...) are
propagated to the other frames of a sequence.

Pure string logic with no OCR or file I/O, so it is easy to unit test.
"""
import calendar
import re

from pydicom.datadict import tag_for_keyword
from pydicom.multival import MultiValue

# A header value must be at least this long to be matched by *containment*
# (the value appearing inside a longer piece of OCR text). Shorter values only
# match a whole word, so that e.g. a two-letter token does not match every
# word that happens to contain it.
CONTAINMENT_MIN_LEN = 4


def normalize(text) -> str:
    """Lower-case and keep letters and digits only: ``'03/14/1962'`` -> ``'03141962'``."""
    return ''.join(ch for ch in str(text).casefold() if ch.isalnum())


def date_display_forms(raw) -> list[str]:
    """Ways a DICOM date (``YYYYMMDD``) is commonly burned into an image.

    The header stores ``19620314`` but an image shows ``03/14/1962``,
    ``14-MAR-1962`` and the like. Returns ``[]`` for anything that is not a
    complete, plausible date.
    """
    raw = str(raw).strip()
    if len(raw) != 8 or not raw.isdigit():
        return []
    y, m, d = raw[:4], raw[4:6], raw[6:]
    if not (1 <= int(m) <= 12 and 1 <= int(d) <= 31):
        return []
    yy, mi, di = y[2:], str(int(m)), str(int(d))
    abbr = calendar.month_abbr[int(m)].upper()
    full = calendar.month_name[int(m)].upper()
    forms = []
    for sep in ('/', '-', '.', ' '):
        for parts in ((m, d, y), (d, m, y), (y, m, d), (mi, di, y), (di, mi, y),
                      (m, d, yy), (d, m, yy), (yy, m, d)):
            forms.append(sep.join(parts))
    for month in (abbr, full):
        forms += [f'{d}-{month}-{y}', f'{d} {month} {y}', f'{d}{month}{y}',
                  f'{month} {d}, {y}', f'{month} {d} {y}', f'{month}-{d}-{y}',
                  f'{d}-{month}-{yy}', f'{d} {month} {yy}']
    forms += [f'{m}{d}{y}', f'{d}{m}{y}', f'{m}{d}{yy}', f'{d}{m}{yy}']
    return list(dict.fromkeys(forms))


def validate_keywords(keywords: list[str]) -> list[str]:
    """Return the keywords that are not valid DICOM data element keywords."""
    return [k for k in keywords if tag_for_keyword(k) is None]


def header_values(datasets, keywords: list[str], min_len: int = 2) -> set[str]:
    """Normalized match values for ``keywords`` across one or more datasets.

    For each header value the whole value is kept, plus its parts (a
    ``PersonName`` ``DOE^JANE`` also yields ``DOE`` and ``JANE``), plus, for
    DA (date) elements, every common burned-in rendering of the date.
    """
    values: set[str] = set()

    def add(text):
        norm = normalize(text)
        if len(norm) >= min_len:
            values.add(norm)

    for ds in datasets:
        for keyword in keywords:
            if keyword not in ds:
                continue
            element = ds.data_element(keyword)
            raw = element.value if element is not None else None
            items = list(raw) if isinstance(raw, (list, MultiValue)) else [raw]
            for item in items:
                if item is None or str(item).strip() == '':
                    continue
                text = str(item)
                add(text)
                for part in re.split(r'[\^\s,;/_\-.]+', text):
                    add(part)
                if getattr(element, 'VR', '') == 'DA':
                    for form in date_display_forms(text):
                        add(form)
    return values


class HeaderMatcher:
    """Decide whether a piece of OCR'd text corresponds to a header value."""

    def __init__(self, values: set[str]):
        self.values = frozenset(values)
        self._long = tuple(v for v in self.values if len(v) >= CONTAINMENT_MIN_LEN)

    def __bool__(self):
        return bool(self.values)

    def matches(self, text) -> bool:
        whole = normalize(text)
        if not whole:
            return False
        if whole in self.values or any(v in whole for v in self._long):
            return True
        # a multi-word entity such as "JANE DOE" matches if any single word does
        return any(normalize(w) in self.values for w in re.split(r'\s+', str(text).strip()))
