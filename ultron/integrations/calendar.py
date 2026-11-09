"""iCloud Calendar over CalDAV. Reads every calendar; writes only to the Ultron one."""

from caldav import DAVClient
from caldav.lib.error import AuthorizationError

from ultron.config import Settings
from ultron.errors import UserFacingError

ICLOUD_CALDAV_URL = "https://caldav.icloud.com"
TIMEOUT_SECONDS = 20


class CalendarError(UserFacingError):
    pass


def find_calendar(names: list[str], wanted: str) -> str | None:
    """Match a calendar name case-insensitively, ignoring surrounding spaces."""
    target = wanted.strip().casefold()
    return next((n for n in names if n.strip().casefold() == target), None)


def check_login(settings: Settings) -> str:
    """Log in, list calendar names, and confirm the Ultron calendar exists."""
    assert settings.icloud_user and settings.icloud_app_password
    try:
        with DAVClient(
            url=ICLOUD_CALDAV_URL,
            username=settings.icloud_user,
            password=settings.icloud_app_password.get_secret_value(),
            timeout=TIMEOUT_SECONDS,
        ) as client:
            names = [
                cal.get_display_name() or "(unnamed)" for cal in client.principal().calendars()
            ]
    except AuthorizationError as exc:
        raise CalendarError(
            "iCloud rejected the login. Check ICLOUD_USER is your Apple ID email and "
            "ICLOUD_APP_PASSWORD is an app-specific password from appleid.apple.com."
        ) from exc

    wanted = settings.icloud_calendar_name
    if find_calendar(names, wanted) is None:
        raise CalendarError(
            f"Logged in, but there's no calendar named '{wanted}'. "
            f"Create it in iCloud. Found: {', '.join(names) or 'none'}"
        )
    return f"{len(names)} calendars, including '{wanted}'"
