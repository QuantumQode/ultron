"""`python -m ultron.setup`: create (or find) the 📥 Inbox and 🗂 Board pages in Notion.

Safe to run any number of times. The bot also does this on startup.
"""

import sys

from pydantic import ValidationError

from ultron.config import get_settings
from ultron.errors import UserFacingError
from ultron.integrations.notion import ensure_workspace_for
from ultron.ops import activity
from ultron.state import State


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        settings = get_settings()
    except ValidationError as exc:
        missing = ", ".join(str(err["loc"][0]).upper() for err in exc.errors())
        sys.exit(f"Can't run setup. Fix these settings in .env: {missing}")
    if not settings.notion_enabled:
        sys.exit("Set NOTION_TOKEN and NOTION_ROOT_PAGE_ID in .env first.")

    state = State(settings.db_path)
    activity.attach(state.db, settings.secret_values())
    try:
        result = ensure_workspace_for(settings, state)
    except UserFacingError as exc:
        sys.exit(f"❌ {exc}")
    finally:
        state.close()

    if result.created:
        print("Created: " + ", ".join(result.created))
    else:
        print("Everything was already in place.")
    print(f"📥 Inbox: {result.workspace.inbox_url}")
    print(f"🗂 Board: {result.workspace.board_url}")
    print(f"📅 Schedule: {result.workspace.schedule_url}")


if __name__ == "__main__":
    main()
