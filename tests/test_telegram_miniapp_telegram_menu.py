from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

SCRIPTS = Path(__file__).parents[1] / "skills" / "telegram" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from telegram_menu import (
    TelegramMenuError,
    TelegramMenuManager,
    normalize_public_url,
    snapshot_owner_chat_id,
)


class FakeTelegram:
    def __init__(self) -> None:
        self.button: dict[str, Any] = {"type": "default"}
        self.set_calls: list[dict[str, Any]] = []
        self.request_timeouts: list[dict[str, float]] = []
        self.fail_after_set = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.request_timeouts.append(dict(request.extensions.get("timeout") or {}))
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content.decode("utf-8"))
        if method == "getMe":
            result: Any = {"id": 99, "username": "owner_bot", "is_bot": True}
        elif method == "getChat":
            result = {"id": 12345, "type": "private"}
        elif method == "getChatMenuButton":
            result = self.button
        elif method == "setChatMenuButton":
            self.button = payload["menu_button"]
            self.set_calls.append(self.button)
            if self.fail_after_set:
                self.fail_after_set = False
                raise httpx.ReadError("lost reply", request=request)
            result = True
        else:
            raise AssertionError(method)
        return httpx.Response(200, json={"ok": True, "result": result})


def manager(tmp_path: Path, remote: FakeTelegram) -> TelegramMenuManager:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(remote.handler),
        base_url="https://api.telegram.test",
    )
    return TelegramMenuManager(
        "secret-token",
        12345,
        tmp_path,
        api_root="https://api.telegram.test",
        client=client,
    )


def test_public_url_is_exact() -> None:
    assert normalize_public_url("https://abc-123.trycloudflare.com") == (
        "https://abc-123.trycloudflare.com/"
    )
    for bad in (
        "http://abc.trycloudflare.com/",
        "https://abc.trycloudflare.com/path",
        "https://abc.trycloudflare.com/?x=1",
        "https://user@abc.trycloudflare.com/",
        "https://trycloudflare.com/",
        "https://abc.trycloudflare.com.attacker.test/",
    ):
        with pytest.raises(TelegramMenuError):
            normalize_public_url(bad)


def test_install_rotate_and_restore_exact_original(tmp_path: Path) -> None:
    remote = FakeTelegram()
    menu = manager(tmp_path, remote)

    async def run() -> None:
        assert await menu.verify_private_owner() == "@owner_bot"
        assert await menu.install("https://first.trycloudflare.com/") is True
        assert await menu.install("https://first.trycloudflare.com/") is False
        assert await menu.install("https://second.trycloudflare.com/") is True
        assert remote.button["web_app"]["url"] == "https://second.trycloudflare.com/"
        assert await menu.restore() is True
        assert remote.button == {"type": "default"}
        assert not menu.snapshot_path.exists()
        await menu._client.aclose()  # type: ignore[union-attr]

    asyncio.run(run())
    assert len(remote.set_calls) == 3


def test_shutdown_restore_bounds_every_telegram_request(tmp_path: Path) -> None:
    remote = FakeTelegram()
    menu = manager(tmp_path, remote)

    async def run() -> None:
        assert await menu.install("https://first.trycloudflare.com/") is True
        remote.request_timeouts.clear()
        assert await menu.restore(request_timeout_sec=0.7) is True
        assert len(remote.request_timeouts) == 3
        assert all(
            values == {"connect": 0.7, "read": 0.7, "write": 0.7, "pool": 0.7}
            for values in remote.request_timeouts
        )
        await menu._client.aclose()  # type: ignore[union-attr]

    asyncio.run(run())


def test_restart_reconciles_lost_reply_after_remote_set(tmp_path: Path) -> None:
    remote = FakeTelegram()
    menu = manager(tmp_path, remote)
    remote.fail_after_set = True

    async def run() -> None:
        with pytest.raises(TelegramMenuError):
            await menu.install("https://first.trycloudflare.com/")
        saved = json.loads(menu.snapshot_path.read_text(encoding="utf-8"))
        assert saved["phase"] == "mutating"
        assert remote.button["web_app"]["url"] == "https://first.trycloudflare.com/"

        # The next start observes the target, finalizes the transaction, and
        # performs no duplicate Telegram mutation.
        assert await menu.install("https://first.trycloudflare.com/") is False
        assert len(remote.set_calls) == 1
        assert await menu.restore() is True
        await menu._client.aclose()  # type: ignore[union-attr]

    asyncio.run(run())


def test_external_change_is_never_overwritten(tmp_path: Path) -> None:
    remote = FakeTelegram()
    menu = manager(tmp_path, remote)

    async def run() -> None:
        await menu.install("https://first.trycloudflare.com/")
        remote.button = {"type": "commands"}
        with pytest.raises(TelegramMenuError, match="outside this skill"):
            await menu.install("https://second.trycloudflare.com/")
        with pytest.raises(TelegramMenuError, match="outside this skill"):
            await menu.restore()
        assert remote.button == {"type": "commands"}
        assert len(remote.set_calls) == 1
        await menu._client.aclose()  # type: ignore[union-attr]

    asyncio.run(run())


def test_exact_original_menu_recovers_stale_installed_snapshot(tmp_path: Path) -> None:
    remote = FakeTelegram()
    menu = manager(tmp_path, remote)

    async def run() -> None:
        await menu.install("https://first.trycloudflare.com/")
        remote.button = {"type": "default"}

        assert await menu.restore() is False
        assert not menu.snapshot_path.exists()
        assert len(remote.set_calls) == 1

        assert await menu.install("https://second.trycloudflare.com/") is True
        saved = json.loads(menu.snapshot_path.read_text(encoding="utf-8"))
        assert saved["original"] == {"type": "default"}
        assert saved["owned"]["web_app"]["url"] == "https://second.trycloudflare.com/"
        assert remote.button == saved["owned"]
        assert len(remote.set_calls) == 2
        await menu._client.aclose()  # type: ignore[union-attr]

    asyncio.run(run())


def test_snapshot_exposes_durable_prior_owner_for_cold_reconciliation(tmp_path: Path) -> None:
    remote = FakeTelegram()
    menu = manager(tmp_path, remote)

    async def run() -> None:
        await menu.install("https://first.trycloudflare.com/")
        assert snapshot_owner_chat_id(tmp_path) == 12345
        await menu._client.aclose()  # type: ignore[union-attr]

    asyncio.run(run())


def test_api_errors_never_expose_token(tmp_path: Path) -> None:
    async def run() -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    400,
                    json={"ok": False, "description": "bad /botsecret-token/path"},
                )
            )
        )
        menu = TelegramMenuManager(
            "secret-token",
            12345,
            tmp_path,
            api_root="https://api.telegram.test",
            client=client,
        )
        with pytest.raises(TelegramMenuError) as captured:
            await menu.current()
        assert "secret-token" not in str(captured.value)
        await client.aclose()

    asyncio.run(run())
