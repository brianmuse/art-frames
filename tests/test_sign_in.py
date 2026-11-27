"""The sign-in form, long sessions, sign-out, the rate limit, first-run setup and LAN host names (auth.py)."""

import base64
import json
import re
import time
from pathlib import Path

import pytest
from test_web import PASSWORD, auth

from art_frames import auth as auth_module
from art_frames.reach import host_allowed, lan_host
from art_frames.web import create_app

DAY = 24 * 3600
NEW_PASSWORD = "correct horse battery staple"


def make(tmp_path, password=PASSWORD, **options):
    return create_app(tmp_path / "data", password=password, testing=True, **options)


def token_of(client):
    """The sign-in and setup form token. A browser gets it from the page; a test puts it in the session."""
    with client.session_transaction() as saved:
        return saved.setdefault("form_token", "test-form-token")


def post_form(client, path, data, **options):
    return client.post(path, data={"form_token": token_of(client), **data}, **options)


def sign_in(client, password=PASSWORD, **extra):
    return post_form(client, "/sign-in", {"password": password, **extra})


def session_cookie(response):
    return next(value for value in response.headers.getlist("Set-Cookie") if value.startswith("session="))


def csrf_of(client):
    with client.session_transaction() as saved:
        return saved["csrf"]


# A password-file install. It keeps working with no change, and it never sees first-run setup.

def test_a_password_file_install_signs_in_with_the_form_and_never_sees_setup(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    # An auth.json someone left behind is ignored: the file wins.
    (root / "auth.json").write_text(json.dumps({"hash": "x", "salt": "eA==", "n": 2, "r": 1, "p": 1}))
    app = make(tmp_path)
    assert app.extensions["auth"].mode == "file"
    client = app.test_client()
    assert client.get("/setup").status_code == 401  # the sign-in form, never the setup form
    response = sign_in(client, next="/household")
    assert response.status_code == 302 and response.headers["Location"] == "/household"
    assert client.get("/household").status_code == 200
    assert not (root / "setup-code").exists()
    # Signed in, /setup only goes Home.
    assert client.get("/setup").headers["Location"] == "/"


@pytest.mark.parametrize("user", ["dayframe", "art-frames"])
def test_basic_auth_still_works_for_scripts(tmp_path, user):
    client = make(tmp_path).test_client()
    header = {"Authorization": "Basic " + base64.b64encode(f"{user}:{PASSWORD}".encode()).decode()}
    assert client.get("/household", headers=header).status_code == 200
    wrong = {"Authorization": "Basic " + base64.b64encode(f"{user}:wrong-password".encode()).decode()}
    response = client.get("/household", headers=wrong)
    assert response.status_code == 401 and "WWW-Authenticate" not in response.headers


def test_an_existing_install_keeps_its_settings_and_session_key(tmp_path):
    root = tmp_path / "data"
    app = make(tmp_path)
    app.extensions["configs"].mutate(lambda raw: raw.update(tv_name="Living Room"))
    settings = (root / "settings.json").read_bytes()
    key = (root / ".session-secret").read_bytes()
    client = app.test_client()
    sign_in(client)
    client.get("/household")
    client.get("/server")
    # A restart (a new app on the same data) uses the same key and settings, and the cookie still works.
    again = make(tmp_path)
    assert (root / ".session-secret").read_bytes() == key == again.config["SECRET_KEY"]
    assert (root / "settings.json").read_bytes() == settings
    assert again.extensions["configs"].load().tv_name == "Living Room"
    assert not (root / "auth.json").exists() and not (root / "updates.json").exists()
    restarted = again.test_client()
    restarted.set_cookie("session", client.get_cookie("session").value)
    assert restarted.get("/household").status_code == 200


# Long sessions.

def test_sign_in_lasts_30_days_from_the_last_visit(tmp_path, monkeypatch):
    client = make(tmp_path).test_client()
    response = sign_in(client)
    cookie = session_cookie(response)
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Expires=" in cookie
    expires = re.search(r"Expires=([^;]+)", cookie).group(1)
    from email.utils import parsedate_to_datetime
    days = (parsedate_to_datetime(expires).timestamp() - time.time()) / DAY
    assert 29.9 < days <= 30.01
    start = time.time()
    # Each visit sends a fresh cookie, so 29 days later it still works, and 29 days after that too.
    for offset in (29, 58):
        monkeypatch.setattr(time, "time", lambda offset=offset: start + offset * DAY)
        response = client.get("/household")
        assert response.status_code == 200
        assert "Set-Cookie" in response.headers
    # 31 days with no visit: sign in again.
    monkeypatch.setattr(time, "time", lambda: start + (58 + 31) * DAY)
    assert client.get("/household").status_code == 401


def test_a_wrong_password_says_so_and_a_cross_site_form_is_refused(tmp_path):
    client = make(tmp_path).test_client()
    response = sign_in(client, password="wrong")
    assert response.status_code == 401 and "That password is not right." in response.text
    response = post_form(client, "/sign-in", {"password": PASSWORD}, headers={"Origin": "http://evil.example"})
    assert response.status_code == 400
    assert client.get("/household").status_code == 401


@pytest.mark.parametrize(("headers", "status"), [
    ({"Origin": "null", "Sec-Fetch-Site": "same-origin"}, 302),
    ({"Sec-Fetch-Site": "none"}, 302),
    ({"Origin": "http://localhost"}, 302),
    ({}, 302),
    ({"Origin": "null", "Sec-Fetch-Site": "cross-site"}, 400),
    ({"Origin": "http://localhost", "Sec-Fetch-Site": "same-site"}, 400),
    # A plain-HTTP home address: browsers may send no Sec-Fetch-Site and "Origin: null". The token decides.
    ({"Origin": "null"}, 302),
    ({"Origin": "http://evil.example"}, 400),
])
def test_the_sign_in_form_works_from_a_real_browser_and_not_from_another_site(tmp_path, headers, status):
    client = make(tmp_path).test_client()
    response = post_form(client, "/sign-in", {"password": PASSWORD}, headers=headers)
    assert response.status_code == status
    assert client.get("/household").status_code == (200 if status == 302 else 401)


@pytest.mark.parametrize("target", ["//evil.example/x", "https://evil.example", "/\\evil.example", "household"])
def test_sign_in_never_sends_you_to_another_site(tmp_path, target):
    client = make(tmp_path).test_client()
    assert sign_in(client, next=target).headers["Location"] == "/"


def test_the_sign_in_page_returns_to_the_page_you_asked_for(tmp_path):
    client = make(tmp_path).test_client()
    page = client.get("/tv/main/schedule?day=mon").text
    assert 'name="next" value="/tv/main/schedule?day=mon"' in page


def test_rate_limit_after_five_wrong_tries(tmp_path):
    app = make(tmp_path)
    now = [1000.0]
    app.extensions["auth"].throttle.clock = lambda: now[0]
    client = app.test_client()
    for _ in range(5):
        assert sign_in(client, password="wrong").status_code == 401
    # The sixth try waits, even with the right password, and Basic auth waits too.
    response = sign_in(client)
    assert response.status_code == 429 and "Too many tries" in response.text
    wrong = {"Authorization": "Basic " + base64.b64encode(b"art-frames:wrong").decode()}
    assert client.get("/household", headers=wrong).status_code == 429
    now[0] += 61
    assert sign_in(client).status_code == 302


def test_csrf_is_still_required_after_a_form_sign_in(tmp_path):
    client = make(tmp_path).test_client()
    sign_in(client)
    assert client.post("/household/timezone", data={"timezone": "UTC"}).status_code == 400
    response = client.post("/household/timezone", data={"timezone": "UTC", "csrf": csrf_of(client)})
    assert response.status_code == 302


def test_a_page_poll_without_a_sign_in_gets_json_so_the_page_reloads(tmp_path):
    client = make(tmp_path).test_client()
    response = client.get("/activity.json", headers={"Accept": "application/json"})
    assert response.status_code == 401 and response.json == {"status": "signed-out"}
    assert response.headers["X-Sign-In"] == "required"


def test_sign_out_and_sign_out_everywhere(tmp_path):
    app = make(tmp_path)
    phone, laptop = app.test_client(), app.test_client()
    sign_in(phone)
    sign_in(laptop)
    response = phone.post("/sign-out", data={"csrf": csrf_of(phone)})
    assert response.headers["Location"] == "/sign-in?done=out"
    assert phone.get("/household").status_code == 401
    assert laptop.get("/household").status_code == 200
    sign_in(phone)
    response = phone.post("/sign-out-everywhere", data={"csrf": csrf_of(phone)})
    assert "You signed out on every device." in phone.get(response.headers["Location"]).text
    assert phone.get("/household").status_code == 401
    assert laptop.get("/household").status_code == 401
    # A password-file install keeps the sign-out version in sessions.json, and the password still works.
    assert json.loads((tmp_path / "data" / "sessions.json").read_text()) == {"format": 1, "sign_out_generation": 2}
    assert sign_in(laptop).status_code == 302 and laptop.get("/household").status_code == 200


def test_a_new_password_file_ends_older_sign_ins(tmp_path):
    client = make(tmp_path).test_client()
    sign_in(client)
    cookie = client.get_cookie("session").value
    other = make(tmp_path, password="another-long-password-for-the-file").test_client()
    other.set_cookie("session", cookie)
    assert other.get("/household").status_code == 401


# First-run setup: no password file and no auth.json.

@pytest.fixture
def fresh(tmp_path):
    app = create_app(tmp_path / "data", testing=True)
    return app, app.test_client(), app.extensions["auth"]


def test_first_run_sends_every_page_to_setup_and_shows_no_app_shell(fresh, tmp_path):
    app, client, signin = fresh
    assert signin.mode == "setup"
    code_file = (tmp_path / "data" / "setup-code").read_text().strip()
    assert code_file == auth_module.show_code(signin.code)
    assert client.get("/").headers["Location"] == "/setup"
    assert client.get("/household").headers["Location"] == "/setup"
    assert client.post("/household/timezone").status_code == 401
    assert client.get("/activity.json", headers={"Accept": "application/json"}).status_code == 401
    assert client.get("/health").json == {"status": "ok"}
    assert client.get("/static/style.css").status_code == 200
    page = client.get("/setup").text
    assert 'name="code"' in page and 'class="sidebar"' not in page and "csrf" not in page
    # The sign-in form is not offered before a password exists.
    assert client.get("/sign-in").headers["Location"] == "/setup"


def test_first_run_creates_a_hashed_password_and_signs_in(fresh, tmp_path):
    app, client, signin = fresh
    code = auth_module.show_code(signin.code)
    # A link from the log fills in the code.
    page = client.get(f"/setup?code={code}").text
    assert "Setup code accepted." in page and f'value="{code}"' in page
    response = post_form(client, "/setup", {"code": "wrong", "password": NEW_PASSWORD, "repeat": NEW_PASSWORD})
    assert response.status_code == 400 and "That setup code is not right." in response.text
    response = post_form(client, "/setup", {"code": code, "password": "short", "repeat": "short"})
    assert response.status_code == 400 and "Use at least 12 characters." in response.text
    response = post_form(client, "/setup", {"code": code, "password": NEW_PASSWORD, "repeat": "different words here"})
    assert "The two passwords are not the same." in response.text
    # Lowercase and without the dash also works: people type it from a log.
    typed = code.replace("-", "").lower()
    response = post_form(client, "/setup", {"code": typed, "password": NEW_PASSWORD, "repeat": NEW_PASSWORD})
    assert response.status_code == 302 and response.headers["Location"] == "/"
    assert "Your password is saved." in client.get("/").text
    root = tmp_path / "data"
    stored = json.loads((root / "auth.json").read_text())
    assert stored["scheme"] == "scrypt" and NEW_PASSWORD not in (root / "auth.json").read_text()
    assert (root / "auth.json").stat().st_mode & 0o077 == 0
    assert not (root / "setup-code").exists() and signin.mode == "stored"
    # The code works once: setup is closed, and the new password signs in with the form and with Basic auth.
    assert client.get("/setup").headers["Location"] == "/"
    stranger = app.test_client()
    response = post_form(stranger, "/setup", {"code": code, "password": "x" * 20, "repeat": "x" * 20})
    assert response.status_code == 401 and signin.check(NEW_PASSWORD) and not signin.check("x" * 20)
    assert sign_in(stranger, NEW_PASSWORD).status_code == 302
    basic = {"Authorization": "Basic " + base64.b64encode(f"art-frames:{NEW_PASSWORD}".encode()).decode()}
    assert app.test_client().get("/household", headers=basic).status_code == 200


@pytest.mark.parametrize(("site", "status"), [("cross-site", 400), ("same-origin", 302)])
def test_first_run_setup_checks_where_the_form_came_from(fresh, site, status):
    app, client, signin = fresh
    code = auth_module.show_code(signin.code)
    response = post_form(client, "/setup", {"code": code, "password": NEW_PASSWORD, "repeat": NEW_PASSWORD},
                           headers={"Origin": "null", "Sec-Fetch-Site": site})
    assert response.status_code == status
    assert signin.mode == ("stored" if status == 302 else "setup")


def test_the_setup_code_expires_after_an_hour(fresh, caplog):
    app, client, signin = fresh
    now = [signin.code_made]
    signin.clock = lambda: now[0]
    old = signin.code
    now[0] += 3601
    response = post_form(client, "/setup", {"code": old, "password": NEW_PASSWORD, "repeat": NEW_PASSWORD})
    assert response.status_code == 400 and "That setup code expired." in response.text
    assert signin.code != old and signin.mode == "setup"
    assert auth_module.show_code(signin.code) in caplog.text


def test_a_restart_in_setup_mode_makes_a_new_code(tmp_path):
    first = create_app(tmp_path / "data", testing=True).extensions["auth"].code
    second = create_app(tmp_path / "data", testing=True).extensions["auth"].code
    assert first != second and len(second) == 10


def test_deleting_auth_json_starts_setup_again(fresh, tmp_path):
    app, client, signin = fresh
    signin.set_password(NEW_PASSWORD, NEW_PASSWORD, setup=True)
    (tmp_path / "data" / "auth.json").unlink()
    again = create_app(tmp_path / "data", testing=True)
    assert again.extensions["auth"].mode == "setup"


def test_change_password_on_the_server_page(fresh):
    app, client, signin = fresh
    signin.set_password(NEW_PASSWORD, NEW_PASSWORD, setup=True)
    other = app.test_client()
    sign_in(client, NEW_PASSWORD)
    sign_in(other, NEW_PASSWORD)
    token = csrf_of(client)
    response = client.post("/server/password", data={"csrf": token, "current": "wrong", "password": "a" * 12,
                                                     "repeat": "a" * 12})
    assert response.status_code == 400 and "Your current password is not right." in response.text
    response = client.post("/server/password", data={"csrf": token, "current": NEW_PASSWORD,
                                                     "password": "a new long password", "repeat": "a new long password"})
    assert response.status_code == 302
    # This device stays signed in; the other one signs in again.
    assert "Password changed." in client.get("/server").text
    assert other.get("/server").status_code == 401
    assert signin.check("a new long password") and not signin.check(NEW_PASSWORD)


def test_a_password_file_install_cannot_change_the_password_in_the_browser(tmp_path):
    client = make(tmp_path).test_client()
    page = client.get("/server", headers=auth()).text
    assert "Your password comes from a file on the server." in page and 'name="current"' not in page


def test_the_demo_needs_no_password(tmp_path):
    client = create_app(tmp_path, demo=True, testing=True).test_client()
    assert client.get("/household").status_code == 200


# Host names on the home network (ART_FRAMES_HOSTS="*", which __main__.py sets for a LAN bind).

@pytest.mark.parametrize("host, ok", [
    ("192.168.1.20:8765", True), ("10.0.0.5", True), ("127.0.0.1:8765", True), ("[::1]:8765", True),
    ("art-frames.local:8765", True), ("nas", True), ("NAS:8765", True), ("[fd00::5]:8765", True),
    ("evil.example", False), ("8.8.8.8", False), ("art-frames.local.evil.example", False), ("", False),
    ("bad_name.local", False),
])
def test_lan_host_names(host, ok):
    assert lan_host(host) is ok
    assert host_allowed(host, ["*"]) is ok


def test_listed_host_names_work_next_to_the_lan_rule():
    assert host_allowed("frames.home.arpa:8765", ["*", "frames.home.arpa"])
    assert not host_allowed("frames.home.arpa", ["localhost"])


def test_a_lan_install_answers_lan_names_and_refuses_others_plainly(tmp_path, monkeypatch):
    monkeypatch.setenv("DAYFRAME_HOSTS", "*")
    client = make(tmp_path).test_client()
    for host in ("192.168.1.20:8765", "art-frames.local:8765"):
        assert client.get("/household", headers={**auth(), "Host": host}).status_code == 200
    response = client.get("/household", headers={**auth(), "Host": "evil.example"})
    assert response.status_code == 400 and 'class="sidebar"' not in response.text
    response = client.get("/", headers={"Host": "evil.example"})
    assert response.status_code == 400 and 'class="sidebar"' not in response.text


# Review fixes (B1, S1, S2, S3, S5, A4, A10, A12, A13, B2, B4).

LAN = "http://192.168.1.20:8765"


def basic(password=PASSWORD, user="dayframe"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def page_token(page):
    return re.search(r'name="form_token" value="([^"]+)"', page).group(1)


@pytest.fixture
def lan(tmp_path, monkeypatch):
    monkeypatch.setenv("ART_FRAMES_HOSTS", "*")
    return tmp_path


def test_sign_in_works_on_a_plain_http_home_address_with_a_null_origin(lan):
    """B1: a browser on http://192.168.1.20 sends no Sec-Fetch-Site and may send "Origin: null"."""
    client = make(lan).test_client()
    page = client.get("/household", base_url=LAN)
    assert page.status_code == 401
    token = page_token(page.text)
    response = client.post("/sign-in", base_url=LAN, headers={"Origin": "null"},
                           data={"form_token": token, "password": PASSWORD, "next": "/household"})
    assert response.status_code == 302 and response.headers["Location"] == "/household"
    assert client.get("/household", base_url=LAN).status_code == 200


@pytest.mark.parametrize(("token", "headers"), [
    (None, {"Origin": "null"}),
    ("wrong", {"Origin": "null"}),
    ("page", {"Origin": "http://evil.example"}),
    ("page", {"Origin": "null", "Sec-Fetch-Site": "cross-site"}),
])
def test_a_sign_in_without_the_page_token_or_from_another_site_is_refused(lan, token, headers):
    client = make(lan).test_client()
    real = page_token(client.get("/sign-in", base_url=LAN).text)
    data = {"password": PASSWORD}
    if token:
        data["form_token"] = real if token == "page" else token
    response = client.post("/sign-in", base_url=LAN, headers=headers, data=data)
    assert response.status_code == 400 and "Open Art Frames again" in response.text
    assert client.get("/household", base_url=LAN).status_code == 401


def test_setup_works_on_a_plain_http_home_address_and_needs_the_token(lan):
    app = create_app(lan / "data", testing=True)
    client = app.test_client()
    code = auth_module.show_code(app.extensions["auth"].code)
    token = page_token(client.get("/setup", base_url=LAN).text)
    words = {"code": code, "password": NEW_PASSWORD, "repeat": NEW_PASSWORD}
    response = client.post("/setup", base_url=LAN, headers={"Origin": "null"}, data=words)
    assert response.status_code == 400 and app.extensions["auth"].mode == "setup"
    response = client.post("/setup", base_url=LAN, headers={"Origin": "http://evil.example"},
                           data={**words, "form_token": token})
    assert response.status_code == 400 and app.extensions["auth"].mode == "setup"
    response = client.post("/setup", base_url=LAN, headers={"Origin": "null"}, data={**words, "form_token": token})
    assert response.status_code == 302 and app.extensions["auth"].mode == "stored"
    assert client.get("/household", base_url=LAN).status_code == 200


def test_the_form_token_is_new_after_sign_in(tmp_path):
    client = make(tmp_path).test_client()
    before = page_token(client.get("/sign-in").text)
    with client.session_transaction() as saved:
        assert saved["form_token"] == before
    assert sign_in(client).status_code == 302
    with client.session_transaction() as saved:
        assert "form_token" not in saved and saved["csrf"]


def test_same_origin_pages_send_their_origin(tmp_path):
    response = make(tmp_path).test_client().get("/sign-in")
    assert response.headers["Referrer-Policy"] == "same-origin"


def test_basic_auth_waits_after_five_wrong_tries_even_with_the_right_password(tmp_path):
    """S1."""
    client = make(tmp_path).test_client()
    for _ in range(5):
        assert client.get("/household", headers=basic("wrong password!!")).status_code == 401
    response = client.get("/household", headers=basic())
    assert response.status_code == 429 and int(response.headers["Retry-After"]) > 0


def test_a_right_basic_sign_in_clears_earlier_wrong_tries(tmp_path):
    client = make(tmp_path).test_client()
    for _ in range(4):
        client.get("/household", headers=basic("wrong password!!"))
    assert client.get("/household", headers=basic()).status_code == 200
    for _ in range(4):
        client.get("/household", headers=basic("wrong password!!"))
    assert client.get("/household", headers=basic()).status_code == 200


@pytest.mark.parametrize("target", [
    "/\t/evil.example", "/\n/x", "/ /evil.example", "/\x7f", "/x\r\nSet-Cookie: a=b",
])
def test_sign_in_refuses_a_next_page_with_spaces_or_control_characters(tmp_path, target):
    """S2."""
    response = sign_in(make(tmp_path).test_client(), next=target)
    assert response.status_code == 302 and response.headers["Location"] == "/"


def test_a_next_page_given_in_the_address_is_checked_too(tmp_path):
    client = make(tmp_path).test_client()
    for query in ("/%0a/x", "/%09/evil.example"):
        response = client.post(f"/sign-in?next={query}", data={"form_token": token_of(client), "password": PASSWORD})
        assert response.status_code == 302 and response.headers["Location"] == "/"
        client.post("/sign-out", data={"csrf": csrf_of(client)})


def test_the_throttle_forgets_the_oldest_addresses_first(monkeypatch):
    """S3."""
    monkeypatch.setattr(auth_module, "KEEP_ADDRESSES", 3)
    throttle = auth_module.Throttle(limit=1, clock=lambda: 100.0)
    for key in ("a", "b", "c"):
        throttle.failed(key)
    throttle.failed("a")  # a is now the newest
    throttle.failed("d")
    assert list(throttle.failures) == ["c", "a", "d"]
    assert throttle.wait("a") and throttle.wait("d") and not throttle.wait("b")


def write_old_record(root, password, n=2**14, **extra):
    """An auth.json from the first version of this branch: n=2**14 and the counter named "version"."""
    salt = b"0123456789abcdef"
    record = {"scheme": "scrypt", "n": n, "r": 8, "p": 1, "salt": base64.b64encode(salt).decode(),
              "hash": base64.b64encode(auth_module._scrypt(password, salt, n, 8, 1)).decode(), **extra}
    root.mkdir(parents=True, exist_ok=True)
    (root / "auth.json").write_text(json.dumps(record))


def test_a_hash_made_with_the_older_scrypt_cost_still_works(tmp_path):
    """S5 and A10: older records keep working; new ones use the higher cost and the new names."""
    root = tmp_path / "data"
    write_old_record(root, NEW_PASSWORD, version=4)
    app = create_app(root, testing=True)
    signin = app.extensions["auth"]
    assert signin.mode == "stored" and signin.check(NEW_PASSWORD) and signin.generation() == 4
    assert sign_in(app.test_client(), NEW_PASSWORD).status_code == 302
    signin.set_password("a new long password", "a new long password", current=NEW_PASSWORD)
    record = json.loads((root / "auth.json").read_text())
    assert record["n"] == 2**15 and record["format"] == 1 and record["sign_out_generation"] == 5
    assert "version" not in record
    signin.sign_out_everywhere()
    record = json.loads((root / "auth.json").read_text())
    assert record["sign_out_generation"] == 6 and "version" not in record


@pytest.mark.parametrize("content", [
    "{not json", "[]", '{"hash": "abc"}',
    '{"scheme": "bcrypt", "n": 16384, "r": 8, "p": 1, "salt": "YQ==", "hash": "YQ=="}',
    '{"scheme": "scrypt", "n": 1073741824, "r": 8, "p": 1, "salt": "YQ==", "hash": "YQ=="}',
    '{"scheme": "scrypt", "n": 1000, "r": 8, "p": 1, "salt": "YQ==", "hash": "YQ=="}',
    '{"scheme": "scrypt", "n": 16384, "r": 8, "p": 1, "salt": "not base64!", "hash": "YQ=="}',
])
def test_a_damaged_auth_json_shows_a_plain_message_not_an_error(tmp_path, content):
    """A10."""
    root = tmp_path / "data"
    root.mkdir()
    (root / "auth.json").write_text(content)
    app = create_app(root, testing=True)
    signin = app.extensions["auth"]
    assert signin.mode == "broken" and not signin.check(NEW_PASSWORD)
    client = app.test_client()
    for path in ("/", "/household", "/sign-in", "/setup"):
        response = client.get(path)
        assert response.status_code == 503 and "ART_FRAMES_RESET_PASSWORD=1" in response.text
        assert 'class="sidebar"' not in response.text and 'name="password"' not in response.text
    assert client.get("/household", headers=basic(NEW_PASSWORD)).status_code == 503
    assert client.get("/health").status_code == 200


def test_reset_password_starts_setup_again_and_keeps_settings(tmp_path, monkeypatch, caplog):
    """A4: the reset for an install with no terminal."""
    root = tmp_path / "data"
    app = create_app(root, testing=True)
    app.extensions["auth"].set_password(NEW_PASSWORD, NEW_PASSWORD, setup=True)
    app.extensions["configs"].mutate(lambda raw: raw.update(tv_name="Kitchen"))
    monkeypatch.setenv("ART_FRAMES_RESET_PASSWORD", "1")
    with caplog.at_level("WARNING"):
        again = create_app(root, testing=True)
    assert again.extensions["auth"].mode == "setup"
    assert (root / "auth-before-reset.json").exists() and (root / "setup-code").exists()
    assert again.extensions["configs"].load().tv_name == "Kitchen"
    assert "remove the variable" in caplog.text
    assert again.test_client().get("/household").headers["Location"] == "/setup"


def test_reset_password_does_nothing_for_a_password_file(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ART_FRAMES_RESET_PASSWORD", "1")
    with caplog.at_level("WARNING"):
        app = make(tmp_path)
    assert app.extensions["auth"].mode == "file"
    assert "does nothing here" in caplog.text
    assert not (tmp_path / "data" / "setup-code").exists()
    assert sign_in(app.test_client()).status_code == 302


def test_reset_password_repairs_a_damaged_auth_json(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    (root / "auth.json").write_text("{not json")
    monkeypatch.setenv("ART_FRAMES_RESET_PASSWORD", "1")
    assert create_app(root, testing=True).extensions["auth"].mode == "setup"


@pytest.mark.parametrize("password", [None, PASSWORD])
def test_an_untrusted_host_gets_a_plain_400_in_every_mode(tmp_path, password):
    """B2 and A12: setup mode used to fail with a 500 here."""
    client = create_app(tmp_path / "data", password=password, testing=True).test_client()
    for path in ("/", "/setup", "/sign-in", "/household"):
        response = client.get(path, headers={"Host": "evil.example"})
        assert response.status_code == 400, path
        assert "ART_FRAMES_HOSTS" in response.text and "Traceback" not in response.text


def test_a_lan_install_names_the_fix_for_an_unknown_host(lan):
    response = make(lan).test_client().get("/", headers={"Host": "evil.example"})
    assert response.status_code == 400
    assert "Use the IP address, or add this name to ART_FRAMES_HOSTS." in response.text


def test_sign_out_ends_a_cached_basic_sign_in_in_that_browser(tmp_path):
    """B4: a browser that once used Basic keeps sending it. After Sign out, it must use the form."""
    client = make(tmp_path).test_client()
    assert client.get("/household", headers=basic()).status_code == 200
    client.post("/sign-out", headers=basic(), data={"csrf": csrf_of(client)})
    assert client.get("/household", headers=basic()).status_code == 401
    assert sign_in(client).status_code == 302
    assert client.get("/household").status_code == 200
    # A script sends no cookie, so Basic still works for it.
    assert make(tmp_path).test_client().get("/household", headers=basic()).status_code == 200


def test_the_server_page_has_one_footer_per_card(tmp_path):
    """A13."""
    app = create_app(tmp_path / "data", testing=True)
    app.extensions["auth"].set_password(NEW_PASSWORD, NEW_PASSWORD, setup=True)
    page = app.test_client().get("/server", headers=basic(NEW_PASSWORD, user="art-frames")).text
    cards = re.findall(r'<section class="card settings-panel" id="([a-z]+)"', page)
    assert cards == ["address", "password", "signout", "updates", "export", "backup"]
    for body in re.findall(r'<section class="card settings-panel".*?</section>', page, re.S):
        assert body.count('class="card-footer') <= 1
    assert 'name="current"' in page
    assert '<rect width="20" height="8" x="2" y="2" rx="2"/>' in page  # the Server icon in the sidebar


def test_first_run_hides_the_log_help_once_the_code_is_accepted(fresh):
    app, client, signin = fresh
    assert "Where is the log?" in client.get("/setup").text
    page = client.get(f"/setup?code={auth_module.show_code(signin.code)}").text
    assert "Setup code accepted." in page and "Where is the log?" not in page


# Fix review round 2.

def test_other_authorization_schemes_are_ignored_and_never_throttle(tmp_path):
    client = make(tmp_path).test_client()
    for _ in range(6):
        assert client.get("/household", headers={"Authorization": "Bearer x"}).status_code == 401
    assert client.get("/household", headers=basic()).status_code == 200


def test_a_sign_in_with_cookies_blocked_says_to_allow_them(tmp_path):
    client = make(tmp_path).test_client(use_cookies=False)
    response = client.post("/sign-in", data={"form_token": "anything", "password": PASSWORD})
    assert response.status_code == 400 and "Allow cookies for this address, then sign in." in response.text


def test_setup_with_cookies_blocked_says_to_allow_them(tmp_path):
    app = create_app(tmp_path / "data", testing=True)
    client = app.test_client(use_cookies=False)
    code = auth_module.show_code(app.extensions["auth"].code)
    response = client.post("/setup", data={"code": code, "password": NEW_PASSWORD, "repeat": NEW_PASSWORD})
    assert response.status_code == 400 and "Allow cookies for this address" in response.text
    assert app.extensions["auth"].mode == "setup"


@pytest.fixture
def unreadable(tmp_path):
    root = tmp_path / "data"
    write_old_record(root, NEW_PASSWORD)
    create_app(root, testing=True)  # an install that ran before, so its other files exist
    (root / "auth.json").chmod(0)
    yield root
    root.chmod(0o700)
    for name in ("auth.json", "auth-before-reset.json"):
        if (root / name).exists():
            (root / name).chmod(0o600)


def test_an_unreadable_auth_json_serves_the_plain_page_and_logs_the_fix(unreadable, caplog):
    with caplog.at_level("ERROR"):
        app = create_app(unreadable, testing=True)
    assert app.extensions["auth"].mode == "broken"
    assert "chown 10001:10001" in caplog.text and "ART_FRAMES_RESET_PASSWORD=1" in caplog.text
    response = app.test_client().get("/household")
    assert response.status_code == 503 and "ART_FRAMES_RESET_PASSWORD=1" in response.text


def test_reset_moves_an_unreadable_auth_json_aside(unreadable, monkeypatch):
    monkeypatch.setenv("ART_FRAMES_RESET_PASSWORD", "1")
    app = create_app(unreadable, testing=True)
    assert app.extensions["auth"].mode == "setup" and (unreadable / "auth-before-reset.json").exists()


def test_reset_says_so_when_it_cannot_move_auth_json(unreadable, monkeypatch, caplog):
    real_replace = Path.replace

    def refuse(self, target):  # as when the data folder belongs to root
        if self.name == "auth.json":
            raise PermissionError(13, "Permission denied")
        return real_replace(self, target)
    monkeypatch.setattr(Path, "replace", refuse)
    monkeypatch.setenv("ART_FRAMES_RESET_PASSWORD", "1")
    with caplog.at_level("WARNING"):
        app = create_app(unreadable, testing=True)
    assert app.extensions["auth"].mode == "broken"
    assert "could not move auth.json aside" in caplog.text and "was not reset" in caplog.text
