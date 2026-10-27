"""验证指定配置启动时的环境隔离、覆盖顺序与失败行为。"""

import os

import pytest

from app.__main__ import main


def test_explicit_config_does_not_merge_local_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("HOST", "PORT", "APP_ENV", "JAVA_CALLBACK_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(
        "PORT=10001\nJAVA_CALLBACK_BASE_URL=http://local.invalid\n", encoding="utf-8"
    )
    production = tmp_path / "production.env"
    production.write_text("APP_ENV=production\nHOST=127.0.0.1\nPORT=13371\n", encoding="utf-8")
    captured = {}

    def capture_run(app, **kwargs):
        captured.update(kwargs, app=app, environment=os.getenv("APP_ENV"))

    monkeypatch.setattr("app.__main__.uvicorn.run", capture_run)
    main(["--env-file", str(production)])

    assert captured == {
        "app": "app.main:app", "host": "127.0.0.1", "port": 13371,
        "workers": 1, "environment": "production",
    }
    assert "JAVA_CALLBACK_BASE_URL" not in os.environ


def test_process_environment_overrides_selected_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PORT", "13372")
    production = tmp_path / "production.env"
    production.write_text("PORT=13371\n", encoding="utf-8")
    ports = []
    monkeypatch.setattr("app.__main__.uvicorn.run", lambda app, **kwargs: ports.append(kwargs["port"]))

    main(["--env-file", str(production)])

    assert ports == [13372]


@pytest.mark.parametrize("target", ["missing.env", "."])
def test_invalid_explicit_config_fails_before_server_start(tmp_path, monkeypatch, target):
    monkeypatch.chdir(tmp_path)
    started = []
    monkeypatch.setattr("app.__main__.uvicorn.run", lambda *args, **kwargs: started.append(True))

    with pytest.raises(SystemExit) as error:
        main(["--env-file", target])

    assert error.value.code == 2
    assert not started
