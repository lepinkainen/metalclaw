import logging

import bot


def test_setup_logging_writes_to_xdg_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    root = logging.getLogger()
    saved, level = list(root.handlers), root.level
    try:
        path = bot._setup_logging()
        logging.getLogger("metalclaw.test").debug("hello-debug")
        for h in root.handlers:
            h.flush()
        assert path == tmp_path / "metalclaw" / "metalclaw.log"
        assert "hello-debug" in path.read_text()
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()
        for h in saved:
            root.addHandler(h)
        root.setLevel(level)
