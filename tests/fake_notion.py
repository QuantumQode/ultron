"""An in-memory stand-in for notion_client.Client, covering the calls Ultron makes."""

import itertools
from typing import Any

import httpx
from notion_client import APIErrorCode, APIResponseError

_ids = itertools.count(1)
_edits = itertools.count(36_000)  # strictly increasing edit times


def not_found(object_id: str) -> APIResponseError:
    return APIResponseError(
        code=APIErrorCode.ObjectNotFound,
        status=404,
        message=f"Could not find {object_id}",
        headers=httpx.Headers(),
        raw_body_text="",
    )


def invalid(message: str) -> APIResponseError:
    return APIResponseError(
        code=APIErrorCode.ValidationError,
        status=400,
        message=f"body failed validation: {message}",
        headers=httpx.Headers(),
        raw_body_text="",
    )


def check_text(block: dict[str, Any]) -> None:
    """Refuse text Notion would refuse: pieces over 2,000 UTF-16 units, over 100 pieces."""
    kind = block["type"]
    body = block.get(kind, {})
    for key in ("rich_text", "caption"):
        parts = body.get(key, [])
        if len(parts) > 100:
            raise invalid(f"{kind}.{key}.length should be ≤ `100`, instead was `{len(parts)}`.")
        for part in parts:
            size = len(part.get("text", {}).get("content", "").encode("utf-16-le")) // 2
            if size > 2000:
                raise invalid(
                    f"{kind}.{key}.text.content.length should be ≤ `2000`, instead was `{size}`."
                )
    for child in body.get("children", []):
        check_text(child)


class FakeNotion:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}  # pages and blocks by id
        self.children: dict[str, list[str]] = {}
        self.calls: list[str] = []
        self.pages = _Pages(self)
        self.blocks = _Blocks(self)
        self.root = self.add_page(None, "Ultron")

    # --- helpers for tests ---

    def add_page(self, parent: str | None, title: str) -> str:
        page_id = f"page-{next(_ids)}"
        self.objects[page_id] = {
            "object": "page",
            "id": page_id,
            "type": "child_page",
            "child_page": {"title": title},
            "in_trash": False,
            "archived": False,
            "last_edited_time": "2026-10-20T09:00:00.000Z",
        }
        self.children[page_id] = []
        if parent:
            self.children[parent].append(page_id)
        return page_id

    def add_block(
        self, parent: str, block: dict[str, Any], after: str | None = None, at_start: bool = False
    ) -> str:
        check_text(block)  # the whole block, nested children too, before anything is added
        block_id = f"block-{next(_ids)}"
        block = dict(block)
        body = dict(block.get(block["type"], {}))
        nested = body.pop("children", [])
        block[block["type"]] = body
        self.objects[block_id] = {"object": "block", "id": block_id, "archived": False, **block}
        self.children[block_id] = []
        siblings = self.children[parent]
        if at_start:
            siblings.insert(0, block_id)
        else:
            siblings.insert(siblings.index(after) + 1 if after else len(siblings), block_id)
        for child in nested:
            self.add_block(block_id, child)
        self.touch(parent)
        return block_id

    def add_text(self, parent: str, kind: str, text: str, **extra: Any) -> str:
        """Add a text block like a paragraph, to_do or bulleted_list_item."""
        body = {"rich_text": [{"type": "text", "text": {"content": text}, "plain_text": text}]}
        return self.add_block(parent, {"type": kind, kind: {**body, **extra}})

    def touch(self, object_id: str, when: str | None = None) -> None:
        """Bump last_edited_time, as Notion does when a page's content changes."""
        obj = self.objects[object_id]
        n = next(_edits)
        obj["last_edited_time"] = (
            when or f"2026-10-20T{n // 3600 % 24:02d}:{n // 60 % 60:02d}:{n % 60:02d}.000Z"
        )

    def trash(self, object_id: str) -> None:
        self.objects[object_id]["in_trash"] = True
        self.objects[object_id]["archived"] = True

    def child_blocks(self, parent: str) -> list[dict[str, Any]]:
        return [
            {**self.objects[i], "has_children": bool(self.children[i])}
            for i in self.children[parent]
            if not self.objects[i].get("archived")
        ]

    def texts(self, parent: str) -> list[str]:
        """Visible text of each live child, for readable assertions."""
        out = []
        for block in self.child_blocks(parent):
            body = block[block["type"]]
            text = "".join(
                t.get("plain_text") or t["text"]["content"] for t in body.get("rich_text", [])
            )
            if block["type"] == "to_do":
                text = ("[x] " if body.get("checked") else "[ ] ") + text
            out.append(text)
        return out

    def get(self, object_id: str) -> dict[str, Any]:
        if object_id not in self.objects:
            raise not_found(object_id)
        return self.objects[object_id]


class _Pages:
    def __init__(self, fake: FakeNotion) -> None:
        self.fake = fake

    def create(self, *, parent, properties, icon=None, children=(), **_):
        self.fake.calls.append("pages.create")
        title = "".join(t["text"]["content"] for t in properties["title"]["title"])
        page_id = self.fake.add_page(parent["page_id"], title)
        self.fake.objects[page_id]["icon"] = icon
        for block in children:
            self.fake.add_block(page_id, block)
        return self.fake.objects[page_id]

    def retrieve(self, *, page_id):
        self.fake.calls.append("pages.retrieve")
        return self.fake.get(page_id)


class _Children:
    def __init__(self, fake: FakeNotion) -> None:
        self.fake = fake

    def list(self, *, block_id, start_cursor=None, page_size=None):
        self.fake.calls.append("blocks.children.list")
        self.fake.get(block_id)
        return {"results": self.fake.child_blocks(block_id), "has_more": False, "next_cursor": None}

    def append(self, *, block_id, children, after=None, position=None, **_):
        self.fake.calls.append("blocks.children.append")
        assert not (after and position), "Notion refuses both after and position"
        if position and position["type"] == "after_block":
            after = position["after_block"]["id"]
        if position and position["type"] == "start":
            ids = []
            for block in reversed(children):  # each goes to the very top
                ids.insert(0, self.fake.add_block(block_id, block, at_start=True))
            siblings = self.fake.children[block_id]
            following = siblings[siblings.index(ids[-1]) + 1 :]
            return {"results": [self.fake.objects[i] for i in ids + following]}
        assert len(children) <= 100, "Notion allows at most 100 blocks per append"
        ids = []
        for block in children:
            ids.append(self.fake.add_block(block_id, block, after=after))
            after = ids[-1]
        # Like the real API: when inserting mid-page, the reply also lists every
        # sibling block that comes after the new ones.
        siblings = self.fake.children[block_id]
        following = siblings[siblings.index(ids[-1]) + 1 :]
        return {"results": [self.fake.objects[i] for i in ids + following]}


class _Blocks:
    def __init__(self, fake: FakeNotion) -> None:
        self.fake = fake
        self.children = _Children(fake)

    def retrieve(self, *, block_id):
        self.fake.calls.append("blocks.retrieve")
        block = self.fake.get(block_id)
        return {**block, "has_children": bool(self.fake.children.get(block_id))}

    def update(self, *, block_id, **payload):
        self.fake.calls.append("blocks.update")
        block = self.fake.get(block_id)
        for kind, body in payload.items():
            assert kind == block["type"], f"can't change a {block['type']} into a {kind}"
            check_text({"type": kind, kind: body})
            block[kind] = {**block[kind], **body}
        return block

    def delete(self, *, block_id):
        self.fake.calls.append("blocks.delete")
        self.fake.get(block_id)["archived"] = True
        return self.fake.get(block_id)
