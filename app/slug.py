def slugify(value: str) -> str:
    """Lowercase the value and turn every space into a hyphen.

    Every other character (apostrophes, colons, digits) is left untouched, so
    "Namit Malhotra's Ramayana" becomes "namit-malhotra's-ramayana".
    """

    return value.lower().replace(" ", "-")
