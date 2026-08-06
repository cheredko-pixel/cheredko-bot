import re


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("380") and len(digits) == 12:
        return digits
    if digits.startswith("0") and len(digits) == 10:
        return "38" + digits
    if digits.startswith("80") and len(digits) == 11:
        return "3" + digits
    return digits
