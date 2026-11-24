import re

from playwright.sync_api import expect


def row(page, name):
    return page.locator(f'tr.row[data-name="{name}"]')


def test_lists_containers_and_filters(page):
    expect(page.locator("tr.row")).to_have_count(4)
    page.fill("#q", "priv")
    expect(page.locator("tr.row")).to_have_count(1)
    page.fill("#q", "")
    page.get_by_role("button", name=re.compile("^Stopped")).click()
    expect(page.locator("tr.row")).to_have_count(1)
    expect(row(page, "old-tool")).to_be_visible()
    page.get_by_role("button", name=re.compile("^All")).click()
    expect(page.locator("tr.row")).to_have_count(4)


def test_check_all_shows_progress_and_summary(page):
    page.click("#btn-check-all")
    expect(page.locator("#runbar")).to_contain_text("finished", timeout=10000)
    expect(page.locator("#runbar")).to_contain_text("update available")
    expect(row(page, "web")).to_contain_text("2.0.0")
    expect(row(page, "web").get_by_role("button", name="Update")).to_be_visible()
    expect(row(page, "db")).to_contain_text("up to date")
    expect(row(page, "private").get_by_role("button", name="Add login")).to_be_visible()
    page.get_by_role("button", name=re.compile("^Updates")).click()
    expect(page.locator("tr.row")).to_have_count(1)


def test_update_from_row(page, up):
    up.check_only("web")
    page.reload()
    row(page, "web").get_by_role("button", name="Update").click()
    expect(row(page, "web")).to_contain_text("updated", timeout=10000)
    assert up.d.ctrs["web"]["Image"] == "sha256:new"


def test_expand_row_shows_details(page):
    row(page, "web").click()
    detail = page.locator("tr.detail")
    expect(detail).to_contain_text("Running")
    expect(detail).to_contain_text("Compose: shop / web")


def test_row_menu(page):
    row(page, "db").locator("button.more").click()
    menu = page.locator("#menu")
    expect(menu).to_be_visible()
    expect(menu).to_contain_text("Versions…")
    page.keyboard.press("Escape")
    expect(menu).to_be_hidden()


def test_versions_dialog_lists_tags(page):
    row(page, "web").locator("button.more").click()
    page.get_by_role("button", name="Versions…").click()
    expect(page.locator("#vsel option")).to_have_count(3)


def test_group_by_project_and_project_run(page):
    page.check("#group")
    grp = page.locator("tr.grp", has_text="shop")
    expect(grp).to_contain_text("2 containers")
    grp.get_by_role("button", name="Check").click()
    expect(page.locator("#runbar")).to_contain_text("finished", timeout=10000)
    expect(page.locator("#runbar")).to_contain_text("2 containers")


def test_add_login_prefills_registry(page, up):
    up.check_only("private")
    page.reload()
    row(page, "private").get_by_role("button", name="Add login").click()
    expect(page.locator("#ldlg")).to_be_visible()
    expect(page.locator("#lhost")).to_have_value("ghcr.io")


def test_logs_panel_filters(page, up):
    up.check_only("db")
    up.check_only("web")
    page.reload()
    page.get_by_role("link", name="Logs").click()
    expect(page.locator(".le").first).to_be_visible()
    page.fill("input[placeholder^='Search messages']", "update available")
    expect(page.locator(".le .m", has_text="update available").first).to_be_visible()


def test_theme_toggle_persists(page):
    assert page.evaluate("document.documentElement.dataset.theme") is None  # auto
    page.click("header button.more")
    page.get_by_role("button", name=re.compile("^Theme: auto")).click()
    assert page.evaluate("document.documentElement.dataset.theme") == "light"
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    page.reload()
    assert page.evaluate("document.documentElement.dataset.theme") == "light"
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == bg == "rgb(244, 246, 249)"


def test_mobile_layout_fits_screen(browser, server, up):
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True)
    p = ctx.new_page()
    p.goto(server)
    p.wait_for_selector("tr.row")
    expect(p.locator("thead")).to_be_hidden()
    assert p.evaluate("document.documentElement.scrollWidth") <= 390
    expect(p.locator('tr.row[data-name="web"]').get_by_role("button", name="Check")).to_be_in_viewport()
    ctx.close()


def test_header_and_table_header_stay_put(browser, server, up):
    for i in range(40):  # enough rows to scroll
        c = dict(up.d.ctrs["db"], Name=f"/extra{i}", Id=f"{i:064d}")
        up.d.ctrs[f"extra{i}"] = c
    ctx = browser.new_context(viewport={"width": 1200, "height": 600})
    p = ctx.new_page()
    p.goto(server)
    p.wait_for_selector('tr.row[data-name="extra39"]')
    p.evaluate("window.scrollTo(0, 1500)")
    assert p.evaluate("window.scrollY") > 500
    top = p.evaluate("document.getElementById('top').getBoundingClientRect().top")
    th = p.evaluate("document.querySelector('thead th').getBoundingClientRect().top")
    height = p.evaluate("document.getElementById('top').offsetHeight")
    assert top == 0 and abs(th - height) <= 1
    ctx.close()


def test_tabs_and_about(page, up):
    nav = page.locator("nav.page-nav")
    expect(nav.locator("a[aria-current=page]")).to_have_text("Containers")
    nav.get_by_role("link", name="About").click()
    expect(page.locator("#tab-containers")).to_be_hidden()
    expect(page.locator("#about-settings")).to_contain_text("Remove old images")
    expect(page.locator("#about-build")).to_contain_text("Docker engine")
    assert page.url.endswith("#about")
    page.reload()  # the tab survives a reload
    expect(nav.locator("a[aria-current=page]")).to_have_text("About")
    nav.get_by_role("link", name="Containers").click()
    row(page, "db").locator("button.more").click()
    page.get_by_role("button", name="Show logs").click()
    expect(page.locator("#tab-logs")).to_be_visible()
    assert page.url.endswith("#logs")


def test_status_icon_and_check_after_update(page, up):
    up.check_only("web")
    page.reload()
    expect(row(page, "web").locator(".sicon.up")).to_have_attribute("title", "update available")
    expect(page.locator("thead th")).to_have_count(4)
    up.update("web")  # an updated container is on the latest version: show the check mark
    page.reload()
    expect(row(page, "web").locator(".sicon.ok")).to_have_attribute("title", "updated")
    expect(row(page, "web").locator(".ver .same")).to_be_visible()


def test_version_opens_build_info(page):
    page.click("#ver")
    dlg = page.locator("#bdlg")
    expect(dlg).to_be_visible()
    expect(dlg.locator("#blist")).to_contain_text("Server started")
    expect(dlg.locator("#blist")).to_contain_text("Python")
    page.keyboard.press("Escape")
    expect(dlg).to_be_hidden()


def test_project_groups_collapse_and_expand(page):
    page.check("#group")
    head = page.locator("tr.grp", has_text="shop").locator("button.gtoggle")
    expect(head).to_have_attribute("aria-expanded", "true")
    expect(row(page, "web")).to_be_visible()
    head.click()
    expect(row(page, "web")).to_have_count(0)
    expect(row(page, "db")).to_have_count(0)
    expect(row(page, "private")).to_be_visible()  # other groups stay open
    page.reload()  # remembered
    expect(row(page, "web")).to_have_count(0)
    page.click("#gexpand")
    expect(row(page, "web")).to_be_visible()
    page.click("#gcollapse")
    expect(page.locator("tr.row")).to_have_count(0)
    expect(page.locator("tr.grp")).to_have_count(2)


def test_cooldown_needs_confirmation_and_password(page, up, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app import main
    fresh = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(up.registry, "remote_info", lambda ref, platform=None: {
        "id": "new", "digest": "sha256:newer", "tags": [], "version": "2.0.0",
        "revision": None, "created": fresh})
    up.cooldown = 86400
    monkeypatch.setattr(main, "UI_PASS", "s3cret")
    up.check_only("web")
    ctx = page.context
    ctx.set_extra_http_headers({"Authorization": "Basic YWRtaW46czNjcmV0"})  # admin:s3cret
    page.reload()
    r = row(page, "web")
    expect(r.locator(".sicon.cool")).to_have_attribute("title", re.compile("in cooldown until"))
    r.get_by_role("button", name="Update…").click()
    dlg = page.locator("#cdlg")
    expect(dlg).to_contain_text("2 hours ago")
    page.fill("#cpass", "wrong")
    page.click("#cgo")
    expect(page.locator("#cmsg")).to_contain_text("Use the Updock sign-in password")
    assert up.d.ctrs["web"]["Image"] != "sha256:new"
    page.fill("#cpass", "s3cret")
    page.click("#cgo")
    expect(dlg).to_be_hidden()
    expect(r).to_contain_text("updated", timeout=10000)
    assert up.d.ctrs["web"]["Image"] == "sha256:new"


def test_major_update_needs_approval(page, up, monkeypatch):
    from app import main
    monkeypatch.setattr(up.registry, "remote_info", lambda ref, platform=None: {
        "id": "new", "digest": "sha256:newer", "tags": [], "version": "9.0.0",
        "revision": None, "created": None})
    monkeypatch.setattr(up, "allow", "minor")
    monkeypatch.setattr(up, "approval_info", lambda name, ctr: None if up.state[name].get("approved") else {
        "from": "1.0.0", "to": "9.0.0", "level": "major", "allow": "minor", "key": "sha256:newer",
        "message": "1.0.0 -> 9.0.0 is a major update; only minor updates are applied without approval"})
    monkeypatch.setattr(main, "UI_PASS", "s3cret")
    up.check_only("web")
    page.context.set_extra_http_headers({"Authorization": "Basic YWRtaW46czNjcmV0"})
    page.reload()
    r = row(page, "web")
    expect(r.locator(".sicon.flag")).to_have_attribute("title", re.compile("needs approval"))
    r.get_by_role("button", name="Approve…").click()
    dlg = page.locator("#cdlg")
    expect(dlg).to_contain_text("major update")
    expect(page.locator("#cgo")).to_have_text("Approve & update")
    page.fill("#cpass", "s3cret")
    page.click("#cgo")
    expect(dlg).to_be_hidden()
    expect(r).to_contain_text("updated", timeout=10000)


def test_delete_container_needs_name_and_password(page, up, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "UI_PASS", "s3cret")
    page.context.set_extra_http_headers({"Authorization": "Basic YWRtaW46czNjcmV0"})
    page.reload()
    row(page, "web").locator("button.more").click()
    page.get_by_role("button", name="Delete container…").click()
    dlg = page.locator("#ddlg")
    expect(dlg).to_contain_text('Compose project "shop"')
    expect(page.locator("#dgo")).to_be_disabled()
    page.fill("#dname", "web")
    expect(page.locator("#dgo")).to_be_enabled()
    page.fill("#dpass", "nope")
    page.click("#dgo")
    expect(page.locator("#dmsg")).to_contain_text("Use the Updock sign-in password")
    assert "web" in up.d.ctrs
    page.fill("#dpass", "s3cret")
    page.click("#dgo")
    expect(dlg).to_be_hidden()
    expect(row(page, "web")).to_have_count(0, timeout=10000)
    assert "web" not in up.d.ctrs


def test_logs_newest_first(page, up):
    import logging
    up._log("first message", container="db", event="check")
    up._log("second message", container="db", event="check")
    up._log("third message", container="db", event="check")
    page.goto(page.url.split("#")[0] + "#logs")
    msgs = page.locator("#logs .le .m")
    expect(msgs.first).to_have_text("third message")
    texts = msgs.all_text_contents()
    assert texts.index("third message") < texts.index("second message") < texts.index("first message")
    up._log("fourth message", logging.WARNING, "db", "check")  # live: arrives on top
    expect(msgs.first).to_have_text("fourth message", timeout=5000)


def test_whats_new_after_upgrade(page, monkeypatch, tmp_path):
    import json

    from app import main
    f = tmp_path / "changelog.json"
    f.write_text(json.dumps({"repository": "me/updock", "releases": [
        {"version": "0.8.0", "date": "2026-11-24", "internal": 3, "sections": [
            {"type": "feat", "title": "Features", "items": [{"text": "richer HTML emails (#62)", "sha": "6444ddf"}]}]},
        {"version": "0.7.0", "date": "2026-11-23", "internal": 0, "sections": [
            {"type": "fix", "title": "Bug fixes", "items": [{"text": "stop autofill", "sha": "abc"}]}]},
        {"version": "0.6.0", "date": "2026-11-22", "internal": 0, "sections": []}]}))
    monkeypatch.setattr(main, "CHANGELOG", f)
    monkeypatch.setattr(main, "VERSION", "0.8.0")
    page.evaluate("localStorage.removeItem('updock.seenVersion')")
    page.reload()  # first visit: remembered, no dialog
    page.wait_for_function("localStorage.getItem('updock.seenVersion') === '0.8.0'")
    expect(page.locator("#wdlg")).to_be_hidden()
    page.evaluate("localStorage.setItem('updock.seenVersion', '0.6.0')")
    page.reload()  # upgraded from 0.6.0
    dlg = page.locator("#wdlg")
    expect(dlg).to_be_visible()
    expect(page.locator("#wtitle")).to_have_text("What's new since v0.6.0")
    expect(dlg).to_contain_text("richer HTML emails")
    expect(dlg).to_contain_text("stop autofill")
    expect(dlg).not_to_contain_text("v0.6.0 2026")
    expect(dlg.locator("a[href='https://github.com/me/updock/pull/62']")).to_be_visible()
    expect(dlg).to_contain_text("+ 3 internal changes")
    page.get_by_role("button", name="Got it").click()
    page.reload()  # seen now: no dialog again
    expect(page.locator("#wdlg")).to_be_hidden()
    page.click("header button.more")
    page.get_by_role("button", name="What's new…").click()
    expect(page.locator("#wtitle")).to_have_text("What's new in Updock")
