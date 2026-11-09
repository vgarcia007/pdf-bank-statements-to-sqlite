"""Complete German IBAN validation outside the original statement parsers."""
import re


def compact(value):
    return ''.join(value.upper().split())


def valid_german_iban(value):
    value = compact(value)
    if not re.fullmatch(r'DE\d{20}', value):
        return False
    rearranged = value[4:] + '1314' + value[2:4]
    return int(rearranged) % 97 == 1


def first_german_iban(pages):
    for page in pages:
        match = re.search(r'(?<![A-Za-z0-9])DE\s*\d{2}(?:\s*\d){18}(?![A-Za-z0-9])', page)
        if match:
            found = compact(match.group())
            return found if valid_german_iban(found) else None
    return None
