"""App Configuration"""

# Django
from django.apps import AppConfig

# AA CharacterScan App
from characterscan import __version__


class CharacterScanConfig(AppConfig):
    """App Config"""

    name = "characterscan"
    label = "characterscan"
    verbose_name = f"Character Scan v{__version__}"
