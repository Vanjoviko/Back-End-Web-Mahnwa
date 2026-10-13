"""Uji pemilihan file env oleh `python -m app`.

Urutan: `ENV_FILE` > `.env.<APP_ENV>` > `.env`. Tidak menyentuh database.
"""
from __future__ import annotations

import os

import app.__main__ as main_module


def test_env_file_explicit_absolute(tmp_path, monkeypatch):
    target = tmp_path / "custom.env"
    target.write_text("A=1")
    monkeypatch.setenv("ENV_FILE", str(target))
    assert main_module.env_file_path() == target


def test_env_file_explicit_relative_to_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "_BASE_DIR", tmp_path)
    monkeypatch.setenv("ENV_FILE", ".env.custom")
    assert main_module.env_file_path() == tmp_path / ".env.custom"


def test_app_env_preferred_over_dot_env(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "_BASE_DIR", tmp_path)
    monkeypatch.delenv("ENV_FILE", raising=False)
    monkeypatch.setenv("APP_ENV", "prod")
    (tmp_path / ".env").write_text("A=1")
    (tmp_path / ".env.prod").write_text("B=2")
    assert main_module.env_file_path() == tmp_path / ".env.prod"


def test_falls_back_to_dot_env(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "_BASE_DIR", tmp_path)
    monkeypatch.delenv("ENV_FILE", raising=False)
    monkeypatch.setenv("APP_ENV", "dev")
    (tmp_path / ".env").write_text("A=1")
    assert main_module.env_file_path() == tmp_path / ".env"


def test_load_dotenv_sets_missing_and_keeps_shell(tmp_path, monkeypatch):
    target = tmp_path / "x.env"
    target.write_text('# komentar\nSOME_KEY=from-file\nOTHER_KEY="kutip-dihapus"\n')
    monkeypatch.setenv("SOME_KEY", "from-shell")
    monkeypatch.delenv("OTHER_KEY", raising=False)
    returned = main_module.load_dotenv(target)
    assert returned == target
    assert os.environ["SOME_KEY"] == "from-shell"  # shell menang
    assert os.environ["OTHER_KEY"] == "kutip-dihapus"


def test_load_dotenv_missing_file_is_noop(tmp_path):
    missing = tmp_path / "tidak-ada.env"
    assert main_module.load_dotenv(missing) == missing
