"""Валидация окружения: бот падает сразу и с понятным русским сообщением."""

import pytest

from config import load_config


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """Полный контроль окружения: значения из настоящего .env не подмешиваются
    (load_dotenv не перекрывает уже установленные переменные)."""
    def set_env(token="123456:TOKEN", admin_ids="1,2", tz="Europe/Moscow",
                **extra):
        monkeypatch.setenv("BOT_TOKEN", token)
        monkeypatch.setenv("ADMIN_IDS", admin_ids)
        monkeypatch.setenv("TIMEZONE", tz)
        monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
        for name in ("BACKUP_DIR", "DATA_DIR", "BACKUP_KEEP_DAYS",
                     "BACKUP_KEEP_WEEKS", "BACKUP_KEEP_MONTHS",
                     "BACKUP_OFFHOST_CMD", "BOT_TOKEN_FILE",
                     "CREDENTIALS_DIRECTORY", "LOG_JSON"):
            monkeypatch.delenv(name, raising=False)
        for name, value in extra.items():
            monkeypatch.setenv(name, value)
    return set_env


def test_valid_env_parsed(clean_env):
    clean_env(admin_ids="10, 20,30")
    config = load_config()
    assert config.admin_ids == frozenset({10, 20, 30})
    assert str(config.tz) == "Europe/Moscow"


def test_missing_token_fails_fast_in_russian(clean_env):
    clean_env(token=" ")
    with pytest.raises(RuntimeError, match="BOT_TOKEN не задан"):
        load_config()


def test_non_numeric_admin_ids_rejected(clean_env):
    clean_env(admin_ids="12,abc")
    with pytest.raises(RuntimeError, match="числовые Telegram ID"):
        load_config()


def test_empty_admin_ids_rejected(clean_env):
    clean_env(admin_ids="  ")
    with pytest.raises(RuntimeError, match="ADMIN_IDS не задан"):
        load_config()


def test_unknown_timezone_rejected(clean_env):
    clean_env(tz="Mars/Olympus")
    with pytest.raises(RuntimeError, match="Неизвестный часовой пояс"):
        load_config()


def test_single_super_admin_is_accepted(clean_env):
    """Ростер из одного человека — рабочая конфигурация, а не отказ старта."""
    clean_env(admin_ids="42")
    assert load_config().admin_ids == frozenset({42})


def test_data_and_backup_dirs_follow_the_database(clean_env, tmp_path):
    clean_env()
    config = load_config()
    assert config.data_dir == str(tmp_path)
    assert config.backup_dir == str(tmp_path / "backups")


def test_backup_retention_from_env(clean_env):
    clean_env(BACKUP_KEEP_DAYS="14", BACKUP_KEEP_WEEKS="6",
              BACKUP_KEEP_MONTHS="24", BACKUP_OFFHOST_CMD="rclone copy {path} r:b")
    config = load_config()
    assert (config.backup_keep_days, config.backup_keep_weeks,
            config.backup_keep_months) == (14, 6, 24)
    assert config.backup_offhost_cmd.startswith("rclone")


@pytest.mark.parametrize("value", ["много", "3.5"])
def test_non_numeric_retention_rejected(clean_env, value):
    clean_env(BACKUP_KEEP_DAYS=value)
    with pytest.raises(RuntimeError, match="целым числом"):
        load_config()


@pytest.mark.parametrize("value", ["0", "-1"])
def test_retention_below_one_rejected(clean_env, value):
    clean_env(BACKUP_KEEP_DAYS=value)
    with pytest.raises(RuntimeError, match="не меньше 1"):
        load_config()


def test_token_can_come_from_a_file(clean_env, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("999:FROMFILE\n", encoding="utf-8")
    clean_env(token="", BOT_TOKEN_FILE=str(token_file))
    assert load_config().bot_token == "999:FROMFILE"


def test_token_can_come_from_systemd_credentials(clean_env, tmp_path):
    """На сервере токен приходит через LoadCredential и не лежит
    в окружении процесса."""
    credentials = tmp_path / "creds"
    credentials.mkdir()
    (credentials / "bot_token").write_text("777:FROMCRED", encoding="utf-8")
    clean_env(token="123:FROMENV", CREDENTIALS_DIRECTORY=str(credentials))
    assert load_config().bot_token == "777:FROMCRED"


def test_missing_token_file_is_reported(clean_env, tmp_path):
    clean_env(token="", BOT_TOKEN_FILE=str(tmp_path / "нет-файла"))
    with pytest.raises(RuntimeError, match="несуществующий файл"):
        load_config()
