import os
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")

from streamlit.testing.v1 import AppTest  # noqa: E402


def test_login_page_renders():
    at = AppTest.from_file("app.py", default_timeout=30).run()
    assert not at.exception
    assert any("MediCore" in t.value for t in at.title)


def test_admin_login_and_dashboard():
    at = AppTest.from_file("app.py", default_timeout=30).run()
    at.text_input[0].set_value("admin")
    at.text_input[1].set_value("admin123")
    at.button[0].click().run()
    assert not at.exception
    assert any("Dashboard" in h.value for h in at.header)


def _login():
    at = AppTest.from_file("app.py", default_timeout=30).run()
    at.text_input[0].set_value("admin")
    at.text_input[1].set_value("admin123")
    at.button[0].click().run()
    return at


def test_every_admin_page_renders():
    at = _login()
    for page in list(at.sidebar.radio[0].options):
        at.sidebar.radio[0].set_value(page).run()
        assert not at.exception, page
