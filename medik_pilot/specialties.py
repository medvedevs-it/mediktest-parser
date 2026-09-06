from typing import Tuple


DEFAULT_SPECIALTY = "Лечебное дело"
SUPPORTED_SPECIALTIES: Tuple[str, ...] = (
    DEFAULT_SPECIALTY,
    "Педиатрия",
)
PACKAGE_YEAR = 2026
PACKAGE_TITLES = {
    DEFAULT_SPECIALTY: "РЭ_Лечебное дело, 2026",
    "Педиатрия": "РЭ_Педиатрия (специалитет), 2026",
}


def normalize_specialty(value: str) -> str:
    """Return the canonical supported specialty name."""
    normalized = " ".join(str(value or "").split()).casefold()
    for specialty in SUPPORTED_SPECIALTIES:
        if specialty.casefold() == normalized:
            return specialty
    raise ValueError(
        "Поддерживаемые специальности: {}.".format(
            ", ".join("«{}»".format(item) for item in SUPPORTED_SPECIALTIES)
        )
    )


def package_title(specialty: str) -> str:
    return PACKAGE_TITLES[normalize_specialty(specialty)]
