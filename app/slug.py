import re

_WHITESPACE_RUN = re.compile(r"\s+")


def slugify(value: str) -> str:
    slug = _WHITESPACE_RUN.sub("-", value.strip().lower())
    return slug.strip("-")
