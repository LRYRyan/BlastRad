"""Dev-only stand-in for pytz (not installable in this sandbox). Not shipped."""
from zoneinfo import ZoneInfo

__version__ = "2000.1"


class _Zone:
    def __init__(self, name):
        self._zone = ZoneInfo(name)

    def localize(self, value):
        return value.replace(tzinfo=self._zone)


def timezone(name):
    return _Zone(name)
