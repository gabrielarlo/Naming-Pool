from __future__ import annotations

import re
import socket
import threading
import time
from contextlib import ExitStack
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Browser, BrowserContext, Page, expect, sync_playwright

from app.config import Settings
from app.main import create_app

pytestmark = pytest.mark.ui

ROOM_CODE = "ui-room-code"
MODERATOR_CODE = "ui-moderator-code"
SALT = "ui-test-salt-0123456789abcdef"
MIN_SUGGESTIONS = 3


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def ui_server(tmp_path: Path):
    settings = Settings(
        room_code=ROOM_CODE,
        moderator_code=MODERATOR_CODE,
        identity_salt=SALT,
        min_suggestions=MIN_SUGGESTIONS,
        db_path=str(tmp_path / "ui-test.db"),
        cookie_secure=False,
    )
    app = create_app(settings)
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        pytest.fail("UI test server did not start")

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        expect.set_options(timeout=10000)
        yield browser
        browser.close()


def open_page(browser: Browser, base_url: str) -> tuple[BrowserContext, Page]:
    context = browser.new_context()
    page = context.new_page()
    page.goto(base_url, wait_until="domcontentloaded")
    expect(page.get_by_role("heading", name="Enter the room")).to_be_visible()
    return context, page


def join(page: Page, code: str = ROOM_CODE) -> None:
    page.get_by_label("Room code").fill(code)
    page.get_by_role("button", name="Join room").click()
    expect(page.get_by_role("heading", name="Add a name and its story")).to_be_visible()


def unlock_moderator(page: Page) -> None:
    page.get_by_text("Moderator access", exact=True).click()
    page.get_by_label("Moderator code").fill(MODERATOR_CODE)
    page.get_by_role("button", name="Unlock controls").click()
    expect(page.get_by_role("heading", name="Moderator panel")).to_be_visible()


def submit(page: Page, name: str, reason: str) -> None:
    page.get_by_label("Name", exact=True).fill(name)
    page.get_by_label("Where it comes from, or why this name").fill(reason)
    page.get_by_role("button", name="Add name").click()
    expect(page.locator(".suggestion-list strong").filter(has_text=name)).to_be_visible()


def click_advance(page: Page, label: str) -> None:
    page.get_by_role("button", name=label).click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("heading", name=label)).to_be_visible()
    dialog.get_by_role("button", name="Confirm").click()


def vote_for(page: Page, names: list[str]) -> None:
    for name in names:
        page.locator(".candidate").filter(has_text=name).click()
    page.get_by_role("button", name="Save votes").click()
    expect(page.locator(".counter")).to_contain_text(f"{len(names)} /")


def test_join_validation_identity_reload_and_no_personal_fields(browser, ui_server):
    with ExitStack() as stack:
        context, page = open_page(browser, ui_server)
        stack.callback(context.close)

        assert page.locator('input[type="email"]').count() == 0
        assert page.get_by_text("No accounts, no email.").is_visible()

        page.get_by_label("Room code").fill("wrong-code")
        page.get_by_role("button", name="Join room").click()
        expect(page.get_by_role("alert")).to_contain_text("Invalid room code.")

        join(page, f"  {ROOM_CODE}  ")
        submit(page, "Reloaded", "Still here after reload.")
        expect(page.locator(".stat").filter(has_text="your names")).to_contain_text(
            "1 / 5"
        )

        page.reload()
        expect(page.get_by_role("heading", name="Add a name and its story")).to_be_visible()
        expect(page.locator(".suggestion-list strong").filter(has_text="Reloaded")).to_be_visible()
        expect(page.locator(".stat").filter(has_text="your names")).to_contain_text(
            "1 / 5"
        )


def test_stale_error_clears_after_recovery(browser, ui_server):
    with ExitStack() as stack:
        context, page = open_page(browser, ui_server)
        stack.callback(context.close)

        state_failed = True

        def handle_state(route):
            nonlocal state_failed
            if state_failed:
                state_failed = False
                route.abort()
                return
            route.fulfill(status=200, content_type="application/json", body="{}")

        context.route("**/api/state", handle_state)

        expect(page.get_by_role("alert")).to_be_visible()
        expect(page.get_by_role("alert")).to_contain_text("Connection lost")

        expect(page.get_by_role("alert")).to_be_hidden()


def test_submission_stats_hidden_names_and_moderator_gate(browser, ui_server):
    with ExitStack() as stack:
        moderator_context, moderator = open_page(browser, ui_server)
        stack.callback(moderator_context.close)
        guest_context, guest = open_page(browser, ui_server)
        stack.callback(guest_context.close)

        join(moderator)
        join(guest)
        expect(guest.locator(".live-indicator")).to_be_visible()
        expect(guest.locator(".live-indicator")).to_contain_text("Live")
        expect(guest.locator(".live-dot")).to_have_class(
            re.compile(r"\bbeat\b"), timeout=5000
        )
        expect(guest.get_by_role("heading", name="Moderator panel")).to_have_count(0)
        expect(guest.get_by_role("dialog")).to_have_count(0)
        expect(guest.locator("dialog")).to_be_hidden()
        unlock_moderator(moderator)
        moderator.reload()
        expect(moderator.get_by_role("heading", name="Moderator panel")).to_be_visible()

        start = moderator.get_by_role("button", name="Start stage 1")
        expect(start).to_be_disabled()
        expect(
            moderator.locator(".hint").filter(
                has_text="3 more needed before stage 1 can start"
            )
        ).to_be_visible()
        expect(
            guest.locator(".stat").filter(has_text="to start").locator("strong")
        ).to_have_text("3 more")

        submit(guest, "Guest Alpha", "Guest reason.")
        expect(guest.locator(".stat").filter(has_text="your names")).to_contain_text(
            "1 / 5"
        )
        for page in (guest, moderator):
            expect(
                page.locator(".stat").filter(has_text="names in the pool").locator("strong")
            ).to_have_text("1")
            expect(
                page.locator(".stat").filter(has_text="to start").locator("strong")
            ).to_have_text("2 more")
        expect(
            moderator.locator(".hint").filter(has_text="2 more needed")
        ).to_be_visible()
        expect(moderator.get_by_text("Guest Alpha", exact=True)).to_have_count(0)

        submit(guest, "Guest Bravo", "Guest reason.")
        submit(moderator, "Mod Charlie", "Moderator reason.")
        for page in (guest, moderator):
            expect(
                page.locator(".stat").filter(has_text="names in the pool").locator("strong")
            ).to_have_text("3")
            expect(
                page.locator(".stat").filter(has_text="to start").locator("strong")
            ).to_have_text("Met")
        expect(guest.get_by_text("Mod Charlie", exact=True)).to_have_count(0)
        expect(start).to_be_enabled()
        expect(
            moderator.locator(".hint").filter(
                has_text="Minimum met; stage 1 can start."
            )
        ).to_be_visible()

        advance_requests = []
        moderator.on(
            "request",
            lambda request: (
                advance_requests.append(request)
                if request.url.endswith("/api/moderator/advance")
                else None
            ),
        )

        start.click()
        dialog = moderator.get_by_role("dialog")
        expect(dialog).to_be_visible()
        expect(dialog.get_by_role("heading", name="Start stage 1")).to_be_visible()
        expect(dialog).to_contain_text("Voting will open")
        expect(dialog).not_to_contain_text("Results will be published")
        expect(dialog.get_by_role("button", name="Cancel")).to_be_focused()
        dialog.get_by_role("button", name="Cancel").click()
        expect(dialog).not_to_be_visible()
        expect(moderator.get_by_role("heading", name="Add a name and its story")).to_be_visible()
        assert advance_requests == []

        start.click()
        expect(dialog).to_be_visible()
        moderator.keyboard.press("Escape")
        expect(dialog).not_to_be_visible()
        expect(moderator.get_by_role("heading", name="Add a name and its story")).to_be_visible()
        assert advance_requests == []

        click_advance(moderator, "Start stage 1")
        expect(moderator.get_by_role("heading", name="Vote for the names you like")).to_be_visible()
        expect(guest.get_by_role("heading", name="Vote for the names you like")).to_be_visible(
            timeout=10000
        )
        assert len(advance_requests) == 1
        for page in (moderator, guest):
            expect(
                page.locator(".stat").filter(has_text="guests voted").locator("strong")
            ).to_have_text("0")
            expect(
                page.locator(".stat").filter(has_text="in the room").locator("strong")
            ).to_have_text("2", timeout=10000)


def test_full_voting_flow_visibility_and_results(browser, ui_server):
    with ExitStack() as stack:
        moderator_context, moderator = open_page(browser, ui_server)
        stack.callback(moderator_context.close)
        g1_context, g1 = open_page(browser, ui_server)
        stack.callback(g1_context.close)
        g2_context, g2 = open_page(browser, ui_server)
        stack.callback(g2_context.close)

        join(moderator)
        join(g1)
        join(g2)
        unlock_moderator(moderator)

        submit(moderator, "Alpha", "Alpha reason.")
        submit(g1, "Bravo", "Bravo reason.")
        submit(g2, "Charlie", "Charlie reason.")
        expect(moderator.get_by_role("button", name="Start stage 1")).to_be_enabled()

        click_advance(moderator, "Start stage 1")
        for page in (moderator, g1, g2):
            expect(page.get_by_role("heading", name="Vote for the names you like")).to_be_visible(
                timeout=10000
            )
            expect(page.get_by_text("Alpha reason.", exact=True)).to_have_count(0)
            expect(page.get_by_role("heading", name="Results")).to_have_count(0)
            expect(
                page.locator(".stat").filter(has_text="guests voted").locator("strong")
            ).to_have_text("0")
            expect(
                page.locator(".stat").filter(has_text="in the room").locator("strong")
            ).to_have_text("3", timeout=10000)

        vote_for(moderator, ["Alpha"])
        vote_for(g1, ["Alpha"])
        vote_for(g2, ["Bravo"])
        expect(
            g2.locator(".stat").filter(has_text="guests voted").locator("strong")
        ).to_have_text("3", timeout=10000)

        g1.reload()
        expect(g1.get_by_role("heading", name="Vote for the names you like")).to_be_visible()
        expect(
            g1.locator('.candidate[aria-pressed="true"]').filter(has_text="Alpha")
        ).to_have_count(1)

        advance_requests = []
        moderator.on(
            "request",
            lambda request: (
                advance_requests.append(request)
                if request.url.endswith("/api/moderator/advance")
                else None
            ),
        )
        moderator.get_by_role("button", name="Close stage 1 and open stage 2").click()
        dialog = moderator.get_by_role("dialog")
        expect(dialog).to_be_visible()
        expect(dialog).to_contain_text("Stage 1 results will be published")
        expect(dialog).to_contain_text("cannot be undone")
        dialog.get_by_role("button", name="Confirm").evaluate(
            "button => { button.click(); button.click(); }"
        )
        for page in (g1, g2):
            expect(page.get_by_role("heading", name="Narrow the top 10 down to 3")).to_be_visible(
                timeout=10000
            )
        assert len(advance_requests) == 1
        expect(moderator.get_by_text("Stage 1 results (names only)")).to_be_visible()
        stage1_block = moderator.locator(".result-block").filter(
            has_text="Stage 1 results"
        )
        expect(stage1_block.locator(".result-reason:visible")).to_have_count(0)
        for page in (moderator, g1, g2):
            expect(page.locator(".candidate").filter(has_text="Alpha")).to_contain_text(
                "Alpha reason."
            )

        vote_for(moderator, ["Alpha"])
        vote_for(g1, ["Alpha"])
        vote_for(g2, ["Bravo"])
        expect(moderator.get_by_text("Stage 2 results (top 10)")).to_have_count(0)

        click_advance(moderator, "Close stage 2 and open the final")
        for page in (g1, g2):
            expect(page.get_by_role("heading", name="Choose the winner")).to_be_visible(
                timeout=10000
            )
        expect(moderator.get_by_text("Stage 2 results (top 10)")).to_be_visible()
        expect(
            moderator.locator(".candidate").filter(has_text="Alpha")
        ).to_contain_text("Alpha reason.")

        vote_for(moderator, ["Alpha"])
        vote_for(g1, ["Alpha"])
        vote_for(g2, ["Bravo"])

        second = moderator.context.new_page()
        stack.callback(second.close)
        second.goto(ui_server, wait_until="domcontentloaded")
        expect(second.get_by_role("heading", name="Choose the winner")).to_be_visible()
        second.get_by_role("button", name="Close the final").click()
        second_dialog = second.get_by_role("dialog")
        expect(second_dialog).to_be_visible()

        click_advance(moderator, "Close the final")

        for page in (moderator, g1, g2, second):
            expect(page.get_by_role("heading", name="Winner: Alpha")).to_be_visible(
                timeout=10000
            )
            expect(page.locator(".winner-card h2")).to_have_text("Alpha")
            expect(page.get_by_text("Final results")).to_be_visible()
            expect(page.locator(".winner-card")).to_contain_text("Alpha reason.")
            expect(page.locator(".result-block").filter(has_text="Final results")).to_be_visible()
            expect(page.get_by_role("heading", name="Moderator panel")).not_to_be_visible()
            expect(page.get_by_text("Stage control", exact=True)).not_to_be_visible()
            expect(page.get_by_text("Moderator access", exact=True)).not_to_be_visible()
            expect(
                page.locator(".stat").filter(has_text="guests voted")
            ).not_to_be_visible()
            expect(
                page.locator(".stat").filter(has_text="in the room")
            ).not_to_be_visible()
        assert second.locator("dialog").evaluate("dialog => dialog.open") is False

        expect(
            moderator.get_by_role("banner").get_by_text("Moderator", exact=True)
        ).not_to_be_visible()

        moderator.reload()
        expect(moderator.get_by_role("heading", name="Winner: Alpha")).to_be_visible()
        expect(moderator.get_by_role("heading", name="Moderator panel")).not_to_be_visible()
        expect(moderator.get_by_text("Moderator access", exact=True)).not_to_be_visible()

        g1.reload()
        expect(g1.get_by_role("heading", name="Winner: Alpha")).to_be_visible()
        expect(g1.get_by_role("heading", name="Moderator panel")).not_to_be_visible()
        expect(g1.get_by_text("Moderator access", exact=True)).not_to_be_visible()

        new_context, new_guest = open_page(browser, ui_server)
        stack.callback(new_context.close)
        new_guest.get_by_label("Room code").fill(ROOM_CODE)
        new_guest.get_by_role("button", name="Join room").click()
        expect(new_guest.get_by_role("heading", name="Winner: Alpha")).to_be_visible(
            timeout=10000
        )
        expect(new_guest.locator(".winner-card h2")).to_have_text("Alpha")
        expect(new_guest.get_by_role("heading", name="Moderator panel")).not_to_be_visible()
        expect(new_guest.get_by_text("Moderator access", exact=True)).not_to_be_visible()
        expect(
            new_guest.locator(".stat").filter(has_text="guests voted")
        ).not_to_be_visible()
        expect(
            new_guest.locator(".stat").filter(has_text="in the room")
        ).not_to_be_visible()


def test_markup_is_rendered_literally(browser, ui_server):
    with ExitStack() as stack:
        context, page = open_page(browser, ui_server)
        stack.callback(context.close)

        join(page)
        submit(page, "<b>Bold</b>", "<script>window.__xss=1</script>")

        expect(page.locator(".suggestion-list strong").filter(has_text="<b>Bold</b>")).to_have_text(
            "<b>Bold</b>"
        )
        expect(page.get_by_text("<script>window.__xss=1</script>", exact=True)).to_be_visible()
        expect(page.locator(".suggestion-list b")).to_have_count(0)
        expect(page.locator(".suggestion-list script")).to_have_count(0)
        assert page.evaluate("window.__xss") is None


def test_tiebreak_ui_resets_and_finishes(browser, ui_server):
    with ExitStack() as stack:
        moderator_context, moderator = open_page(browser, ui_server)
        stack.callback(moderator_context.close)
        g1_context, g1 = open_page(browser, ui_server)
        stack.callback(g1_context.close)
        g2_context, g2 = open_page(browser, ui_server)
        stack.callback(g2_context.close)

        join(moderator)
        join(g1)
        join(g2)
        unlock_moderator(moderator)

        submit(moderator, "Alpha", "Alpha reason.")
        submit(g1, "Bravo", "Bravo reason.")
        submit(g2, "Charlie", "Charlie reason.")

        click_advance(moderator, "Start stage 1")
        for page in (moderator, g1, g2):
            expect(page.get_by_role("heading", name="Vote for the names you like")).to_be_visible(
                timeout=10000
            )
        click_advance(moderator, "Close stage 1 and open stage 2")
        for page in (moderator, g1, g2):
            expect(page.get_by_role("heading", name="Narrow the top 10 down to 3")).to_be_visible(
                timeout=10000
            )
        click_advance(moderator, "Close stage 2 and open the final")
        for page in (moderator, g1, g2):
            expect(page.get_by_role("heading", name="Choose the winner")).to_be_visible(
                timeout=10000
            )

        vote_for(moderator, ["Alpha"])
        vote_for(g1, ["Bravo"])
        vote_for(g2, ["Charlie"])
        click_advance(moderator, "Close the final")
        for page in (moderator, g1, g2):
            expect(page.get_by_role("heading", name="Break the tie")).to_be_visible(
                timeout=10000
            )

        vote_for(moderator, ["Alpha"])
        vote_for(g1, ["Bravo"])
        vote_for(g2, ["Charlie"])
        click_advance(moderator, "Close the tiebreak")
        expect(moderator.get_by_role("status")).to_contain_text("still tied")
        expect(moderator.get_by_role("heading", name="Break the tie")).to_be_visible()
        for page in (moderator, g1, g2):
            expect(page.locator('.candidate[aria-pressed="true"]')).to_have_count(0)

        vote_for(moderator, ["Alpha"])
        vote_for(g1, ["Alpha"])
        vote_for(g2, ["Bravo"])
        click_advance(moderator, "Close the tiebreak")
        expect(moderator.get_by_role("heading", name="Winner: Alpha")).to_be_visible(
            timeout=10000
        )
        expect(moderator.locator(".winner-card h2")).to_have_text("Alpha")
