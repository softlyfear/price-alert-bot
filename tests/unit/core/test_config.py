"""Construct nested settings explicitly and disable dotenv to isolate host
configuration.
"""

from pathlib import Path
from urllib.parse import unquote
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pytest
from pydantic import BaseModel
from pydantic import SecretStr
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from app.core.config import AppSettings
from app.core.config import BotSecret
from app.core.config import DatabaseSettings
from app.core.config import OzonSettings
from app.core.config import RedisSettings
from app.core.config import Settings
from app.core.config import WbSettings
from app.core.config import get_settings

# A password containing every special character named by the ticket
# (`@ : / # % ? & + =`), a literal space, and Cyrillic, used everywhere a
# test needs to prove percent-encoding survives a roundtrip rather than
# just "looking fine" on a simple `pass123`-style password.
_SPECIAL_CHARS_PASSWORD = "p@ss/w:o#r%d?a&b+c= пароль"


def _make_db_settings(
    *, password: str = "db-marker-pass", host: str = "db-host"
) -> DatabaseSettings:
    """Build test settings without an external database."""
    return DatabaseSettings(PATH=Path(host))


def _make_bot_secret(*, token: str = "bot-marker-token") -> BotSecret:
    """See ``_make_db_settings`` for why ``token`` is passed as ``str``."""
    return BotSecret(BOT_TOKEN=token)  # type: ignore[arg-type]


def _make_redis_settings(
    *, host: str = "redis-host", password: str | None = None
) -> RedisSettings:
    """See ``_make_db_settings`` for why ``password`` is passed as ``str``."""
    return RedisSettings(
        HOST=host,
        PASSWORD=password,  # type: ignore[arg-type]
    )


# --- Defaults -----------------------------------------------------------


def test_app_settings_defaults_when_no_fields_given() -> None:
    app = AppSettings()

    assert app.SCHEDULER_INTERVAL_SECONDS == 900
    assert app.ALERT_COOLDOWN_SECONDS == 86400
    assert app.MAX_PRODUCTS_PER_USER == 50
    assert app.MARKETPLACE_CONCURRENCY == 5
    assert app.HTTP_TIMEOUT_SECONDS == 10.0


def test_redis_settings_defaults_when_only_host_given() -> None:
    redis = RedisSettings(HOST="cache")

    assert redis.PORT == 6379
    assert redis.DB == 0
    assert redis.PASSWORD is None


# --- Required field: REDIS__HOST ----------------------------------------


def test_redis_settings_missing_host_raises_missing_field_error() -> None:
    with pytest.raises(ValidationError) as excinfo:
        RedisSettings()  # type: ignore[call-arg]

    errors = excinfo.value.errors()
    assert any(e["type"] == "missing" and e["loc"] == ("HOST",) for e in errors)


# --- HOST content, not just length (agreement 13) ------------------------


def test_redis_settings_empty_host_rejected_as_too_short() -> None:
    with pytest.raises(ValidationError) as excinfo:
        RedisSettings(HOST="")

    errors = excinfo.value.errors()
    assert any(
        e["type"] == "string_too_short" and e["loc"] == ("HOST",) for e in errors
    )


@pytest.mark.parametrize("blank_host", [" ", "  \t "], ids=["single-space", "tabs"])
def test_redis_settings_whitespace_only_host_rejected_by_custom_validator(
    blank_host: str,
) -> None:
    """`min_length=1` alone would accept these -- both are non-empty
    strings. Rejection here comes from the project's own ``field_validator``
    (``_reject_blank_host``), a separate mechanism with its own error type.
    """
    with pytest.raises(ValidationError) as excinfo:
        RedisSettings(HOST=blank_host)

    errors = excinfo.value.errors()
    assert any(e["type"] == "value_error" and e["loc"] == ("HOST",) for e in errors)


def test_redis_settings_host_with_surrounding_whitespace_is_accepted_verbatim() -> None:
    """Nonblank host values are retained verbatim rather than normalized."""
    redis = RedisSettings(HOST=" host ")

    assert redis.HOST == " host "


# --- PORT boundaries (both sides) ----------------------------------------


def test_redis_settings_port_lower_boundary_zero_rejected_one_accepted() -> None:
    with pytest.raises(ValidationError) as excinfo:
        RedisSettings(HOST="h", PORT=0)
    errors = excinfo.value.errors()
    assert any(
        e["type"] == "greater_than_equal" and e["loc"] == ("PORT",) for e in errors
    )

    assert RedisSettings(HOST="h", PORT=1).PORT == 1


def test_redis_settings_port_upper_boundary_65535_accepted_65536_rejected() -> None:
    assert RedisSettings(HOST="h", PORT=65535).PORT == 65535

    with pytest.raises(ValidationError) as excinfo:
        RedisSettings(HOST="h", PORT=65536)
    errors = excinfo.value.errors()
    assert any(e["type"] == "less_than_equal" and e["loc"] == ("PORT",) for e in errors)


# --- DB boundary -----------------------------------------------------------


def test_redis_settings_db_lower_boundary_negative_one_rejected_zero_accepted() -> None:
    with pytest.raises(ValidationError) as excinfo:
        RedisSettings(HOST="h", DB=-1)
    errors = excinfo.value.errors()
    assert any(
        e["type"] == "greater_than_equal" and e["loc"] == ("DB",) for e in errors
    )

    assert RedisSettings(HOST="h", DB=0).DB == 0


# --- AppSettings gt=0 boundaries ------------------------------------------


@pytest.mark.parametrize(
    "field_name",
    [
        "SCHEDULER_INTERVAL_SECONDS",
        "ALERT_COOLDOWN_SECONDS",
        "MAX_PRODUCTS_PER_USER",
        "MARKETPLACE_CONCURRENCY",
    ],
)
def test_app_settings_gt_zero_fields_reject_zero_and_negative_but_accept_one(
    field_name: str,
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        AppSettings.model_validate({field_name: 0})
    errors = excinfo.value.errors()
    assert any(
        e["type"] == "greater_than" and e["loc"] == (field_name,) for e in errors
    )

    with pytest.raises(ValidationError) as excinfo:
        AppSettings.model_validate({field_name: -1})
    errors = excinfo.value.errors()
    assert any(
        e["type"] == "greater_than" and e["loc"] == (field_name,) for e in errors
    )

    assert getattr(AppSettings.model_validate({field_name: 1}), field_name) == 1


# --- HTTP_TIMEOUT_SECONDS: inf/nan and the two-sided upper boundary ------
# (agreement 12: gt/le alone do not reject infinity)


@pytest.mark.parametrize(
    ("value", "expected_value", "expected_error_type"),
    [
        pytest.param(float("inf"), None, "finite_number", id="float-inf"),
        pytest.param(float("-inf"), None, "finite_number", id="float-neg-inf"),
        pytest.param(float("nan"), None, "finite_number", id="float-nan"),
        pytest.param(0, None, "greater_than", id="zero"),
        pytest.param(-1, None, "greater_than", id="negative"),
        pytest.param(60, 60.0, None, id="upper-bound-accepted"),
        pytest.param(60.0000001, None, "less_than_equal", id="just-above-upper-bound"),
        pytest.param(61, None, "less_than_equal", id="above-upper-bound"),
        pytest.param(10.0, 10.0, None, id="typical-value-accepted"),
    ],
)
def test_http_timeout_seconds_accepts_and_rejects_boundary_and_non_finite_values(
    value: float,
    expected_value: float | None,
    expected_error_type: str | None,
) -> None:
    if expected_error_type is None:
        accepted = AppSettings(HTTP_TIMEOUT_SECONDS=value)
        assert expected_value == accepted.HTTP_TIMEOUT_SECONDS
        return

    with pytest.raises(ValidationError) as excinfo:
        AppSettings(HTTP_TIMEOUT_SECONDS=value)
    errors = excinfo.value.errors()
    assert any(
        e["type"] == expected_error_type and e["loc"] == ("HTTP_TIMEOUT_SECONDS",)
        for e in errors
    )


@pytest.mark.parametrize(
    "value", ["inf", "1e400"], ids=["str-inf", "str-1e400-overflow"]
)
def test_http_timeout_seconds_rejects_non_finite_string_encoded_values(
    value: str,
) -> None:
    """``'inf'`` parses as a valid float and ``'1e400'`` silently overflows
    to ``inf`` in Python's own ``float()`` -- both must still be rejected by
    ``allow_inf_nan=False``, exactly like their numeric counterparts above.
    """
    with pytest.raises(ValidationError) as excinfo:
        AppSettings(HTTP_TIMEOUT_SECONDS=value)  # type: ignore[arg-type]
    errors = excinfo.value.errors()
    assert any(
        e["type"] == "finite_number" and e["loc"] == ("HTTP_TIMEOUT_SECONDS",)
        for e in errors
    )


# --- RedisSettings.REDIS_URL -----------------------------------------------


def test_redis_url_without_password() -> None:
    redis = _make_redis_settings(host="cache", password=None)

    assert redis.REDIS_URL == "redis://cache:6379/0"


def test_redis_url_with_empty_password_has_no_auth_section() -> None:
    """`PASSWORD=""` means "no password" (matches Redis's own ``requirepass
    ""``), not an empty-but-present credential. Regression risk named by the
    ticket: swapping the "password is truthy" check for a "password is not
    None" check would silently add an empty ``:@`` auth section.
    """
    redis = RedisSettings(HOST="h", PASSWORD=SecretStr(""))

    assert redis.REDIS_URL == "redis://h:6379/0"
    assert "@" not in redis.REDIS_URL


def test_redis_url_with_special_character_password_roundtrips() -> None:
    """An unescaped slash in a password breaks URL netloc parsing."""
    redis = RedisSettings(
        HOST="cache-host", PORT=6380, DB=2, PASSWORD=SecretStr(_SPECIAL_CHARS_PASSWORD)
    )

    url = redis.REDIS_URL
    parts = urlsplit(url)

    assert parts.hostname == "cache-host"
    assert parts.port == 6380
    assert parts.path == "/2"
    assert parts.password is not None
    assert unquote(parts.password) == _SPECIAL_CHARS_PASSWORD


# --- DatabaseSettings.DATABASE_URL -----------------------------------------
# Existed since the start of the project, 0% covered before this ticket.


def test_database_url_roundtrips_special_characters_in_path() -> None:
    db = DatabaseSettings(PATH=Path("/tmp/a %25 database.sqlite3"))
    parsed = make_url(db.DATABASE_URL)
    assert parsed.drivername == "sqlite+aiosqlite"
    assert parsed.database == "/tmp/a %25 database.sqlite3"


def test_bot_token_secret_not_leaked_in_repr_str_and_dumps() -> None:
    marker = "tg-marker-do-not-leak"
    tg = _make_bot_secret(token=marker)

    assert marker not in repr(tg)
    assert marker not in str(tg)
    assert marker not in str(tg.model_dump())
    assert marker not in tg.model_dump_json()


def test_redis_password_secret_not_leaked_in_repr_str_and_dumps() -> None:
    marker = "redis-marker-do-not-leak"
    redis = _make_redis_settings(host="h", password=marker)

    assert marker not in repr(redis)
    assert marker not in str(redis)
    assert marker not in str(redis.model_dump())
    assert marker not in redis.model_dump_json()


# --- Secret hidden in ValidationError text, guarded through Settings ------
# (ticket points 1, 1a, 1b: two independent mutations must each turn this
# red -- demonstrated separately, on a copy outside the working tree, per
# agreement 7/8; see the task report.)


def test_missing_redis_host_validation_error_hides_raw_password_via_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validate through Settings: a raw nested dict reaches the pre-coercion missing-
    field path.
    """
    monkeypatch.delenv("REDIS__HOST", raising=False)
    marker = "redis-marker-secret-do-not-leak"
    # Built outside the `with` block on purpose: `_make_db_settings()` and
    # `_make_bot_secret()` each construct and validate a model of their
    # own, and a `ValidationError` raised by either of them would be
    # swallowed by the same `pytest.raises` block below, silently
    # replacing the one this test actually means to inspect.
    db = _make_db_settings()
    tg = _make_bot_secret()

    with pytest.raises(ValidationError) as excinfo:
        Settings(
            _env_file=None,
            db=db,
            tg=tg,
            redis={"PASSWORD": marker},  # type: ignore[arg-type]
        )

    errors = excinfo.value.errors()
    assert any(e["type"] == "missing" and e["loc"] == ("redis", "HOST") for e in errors)
    assert marker not in str(excinfo.value)


def test_hide_input_in_errors_also_hides_raw_value_for_non_secret_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hiding validation input also applies to non-secret fields."""
    monkeypatch.delenv("DB__BUSY_TIMEOUT_SECONDS", raising=False)
    bad_port_marker = "not-a-number-marker"
    malformed_db = {"BUSY_TIMEOUT_SECONDS": bad_port_marker}
    # Built outside the `with` block for the same reason as in
    # `test_missing_redis_host_validation_error_hides_raw_password_via_settings`
    # above: a `ValidationError` from either factory call would otherwise
    # be caught by the same `pytest.raises` block and mistaken for the one
    # under test.
    tg = _make_bot_secret()
    redis = _make_redis_settings(host="h")

    with pytest.raises(ValidationError) as excinfo:
        Settings(
            _env_file=None,
            db=malformed_db,  # type: ignore[arg-type]
            tg=tg,
            redis=redis,
        )

    errors = excinfo.value.errors()
    assert any(
        e["type"] == "float_parsing" and e["loc"] == ("db", "BUSY_TIMEOUT_SECONDS")
        for e in errors
    )
    assert bad_port_marker not in str(excinfo.value)


# --- APP__* partial override: default_factory must not suppress env reads -


def test_app_settings_env_var_partial_override_keeps_other_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use a non-default override and clear other variables to isolate host
    configuration.
    """
    monkeypatch.setenv("APP__MAX_PRODUCTS_PER_USER", "7")
    for var in (
        "APP__SCHEDULER_INTERVAL_SECONDS",
        "APP__ALERT_COOLDOWN_SECONDS",
        "APP__MARKETPLACE_CONCURRENCY",
        "APP__HTTP_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)

    settings = Settings(
        _env_file=None,
        db=_make_db_settings(),
        tg=_make_bot_secret(),
        redis=_make_redis_settings(host="h"),
    )

    assert settings.app.MAX_PRODUCTS_PER_USER == 7
    assert settings.app.SCHEDULER_INTERVAL_SECONDS == 900
    assert settings.app.ALERT_COOLDOWN_SECONDS == 86400
    assert settings.app.MARKETPLACE_CONCURRENCY == 5
    assert settings.app.HTTP_TIMEOUT_SECONDS == 10.0


# --- get_settings(): the one call site that must read the environment ----


# Prefix -> nested settings model, used to clear the whole prefix space
# (convention 19) by reading field names off ``model_fields`` directly
# rather than off a hand-maintained tuple: a field added to one of these
# four models later is picked up automatically, instead of silently
# reintroducing a dependency on the calling shell the next time someone
# adds a field and forgets this list exists.
_SETTINGS_PREFIX_MODELS: tuple[tuple[str, type[BaseModel]], ...] = (
    ("DB__", DatabaseSettings),
    ("TG__", BotSecret),
    ("REDIS__", RedisSettings),
    ("APP__", AppSettings),
    ("WB__", WbSettings),
    ("OZON__", OzonSettings),
)


def _delenv_settings_prefix_space(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear entire prefixes so ambient variables cannot merge into test settings."""
    for prefix, model in _SETTINGS_PREFIX_MODELS:
        for field_name in model.model_fields:
            monkeypatch.delenv(f"{prefix}{field_name}", raising=False)


def _set_env_for_get_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set every environment variable ``get_settings()`` needs to build a
    valid ``Settings`` starting from a completely empty environment.

    Shared by the two tests below that exercise ``get_settings()`` itself
    -- the one call site in this file that must read the environment by
    construction, since it calls ``Settings()`` with no overrides at all.

    Clears the whole prefix space first (convention 19) so a stray shell
    variable outside the set below cannot merge in underneath it.
    """
    _delenv_settings_prefix_space(monkeypatch)
    monkeypatch.setenv("DB__PATH", "db-host")
    monkeypatch.setenv("DB__BUSY_TIMEOUT_SECONDS", "12")
    monkeypatch.setenv("TG__BOT_TOKEN", "tg-marker-token")
    monkeypatch.setenv("REDIS__HOST", "redis-host")


def test_get_settings_builds_settings_from_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get_settings()`` calls ``Settings()`` with no overrides at all, by
    design -- it is the production entry point, and reading the environment
    is its entire job. That means it cannot be exercised the way every other
    test in this file is (by passing pre-built instances); every required
    variable is supplied through ``monkeypatch.setenv``, restored
    automatically once the test ends.

    ``get_settings.__wrapped__()`` calls the undecorated function body
    directly, bypassing ``@lru_cache``. Calling ``get_settings()`` itself
    would cache the resulting ``Settings`` instance for the lifetime of the
    test process, potentially handing a stale, test-only instance to any
    other test that later calls ``get_settings()`` -- ``__wrapped__`` avoids
    that entirely.

    ``monkeypatch.setitem(Settings.model_config, "env_file", None)``
    disables dotenv lookup for the duration of this test. Every other test
    in this file gets the same protection for free by passing
    ``_env_file=None`` to ``Settings(...)`` directly; this call goes
    through ``Settings()`` with no arguments, so the only way to reach the
    same guarantee is to mutate ``model_config`` itself. Without it, a real
    ``.env`` present on the machine running the suite would be merged in
    alongside the monkeypatched variables, and a malformed value in that
    file (e.g. ``APP__HTTP_TIMEOUT_SECONDS=not-a-number``) would fail this
    test on a developer's machine while passing in CI, or vice versa.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    _set_env_for_get_settings(monkeypatch)

    settings = get_settings.__wrapped__()

    assert str(settings.db.PATH) == "db-host"
    assert settings.tg.BOT_TOKEN.get_secret_value() == "tg-marker-token"
    assert settings.redis.HOST == "redis-host"


def test_get_settings_is_cached_across_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``@lru_cache`` on ``get_settings`` means the environment is read and
    validated once per process; every other test in this file deliberately
    bypasses that cache via ``get_settings.__wrapped__()`` so as not to
    pollute other tests with a test-only instance -- which also means
    nothing in this file previously asserted that the caching itself
    works. Removing ``@lru_cache`` from ``get_settings`` would make this
    test fail (two distinct ``Settings`` instances) while leaving every
    other test in this file green, since they never call the decorated
    function.

    The cache is cleared both before and after the two calls under test:
    before, so a stale instance possibly left by another test (or a
    previous run of this one) cannot make the assertion pass for the wrong
    reason; after, so this test does not leak a cached instance -- built
    from monkeypatched, test-only values -- into whichever test runs next
    and calls ``get_settings()`` for real.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    _set_env_for_get_settings(monkeypatch)
    get_settings.cache_clear()

    try:
        first = get_settings()
        second = get_settings()
    finally:
        get_settings.cache_clear()

    assert first is second


# --- Settings: all four sections built from explicit values ---------------


def test_settings_builds_from_explicit_values_across_all_sections() -> None:
    """All four top-level sections -- ``db``, ``tg``, ``redis``, ``app`` --
    are passed as fully-built instances and every one of them must reach
    the resulting ``Settings`` object unchanged. Every field checked below
    is given a value distinct from every other field's value and from
    every field's default, specifically so that a section assigned to the
    wrong attribute (e.g. ``Settings(db=redis, redis=db, ...)`` from a
    copy-paste mistake, or a keyword silently renamed) fails on a mismatch
    instead of by accident matching some other section's value.

    This test is not the only guard against a ``db``/``redis`` swap at the
    ``Settings`` level -- measured on a copy with the section types on the
    model itself swapped (``db: RedisSettings`` / ``redis: DatabaseSettings``):
    three other tests in this file turn red too --
    ``test_app_settings_env_var_partial_override_keeps_other_defaults``,
    ``test_get_settings_builds_settings_from_environment_variables``, and
    ``test_get_settings_is_cached_across_calls`` -- because pydantic itself
    rejects the mismatched section types before any field-level assertion
    here runs (``4 failed, 34 passed``). What this test's distinct-value
    setup earns on top of that is precision: if a future change ever made
    the section types compatible (both plain ``BaseModel`` subclasses with
    overlapping fields, say), a swap would stop being a pydantic-level
    error and become a silent value mismatch -- this is the one test in the
    file positioned to catch that narrower case, by construction.
    """
    db = _make_db_settings(host="explicit-db-host")
    tg = _make_bot_secret(token="explicit-bot-token")
    redis = _make_redis_settings(host="explicit-redis-host")
    app = AppSettings(
        SCHEDULER_INTERVAL_SECONDS=111,
        ALERT_COOLDOWN_SECONDS=222,
        MAX_PRODUCTS_PER_USER=33,
        MARKETPLACE_CONCURRENCY=4,
        HTTP_TIMEOUT_SECONDS=12.5,
    )

    settings = Settings(_env_file=None, db=db, tg=tg, redis=redis, app=app)

    assert str(settings.db.PATH) == "explicit-db-host"
    assert settings.tg.BOT_TOKEN.get_secret_value() == "explicit-bot-token"
    assert settings.redis.HOST == "explicit-redis-host"
    assert settings.app.SCHEDULER_INTERVAL_SECONDS == 111
    assert settings.app.ALERT_COOLDOWN_SECONDS == 222
    assert settings.app.MAX_PRODUCTS_PER_USER == 33
    assert settings.app.MARKETPLACE_CONCURRENCY == 4
    assert settings.app.HTTP_TIMEOUT_SECONDS == 12.5


# --- PAB-079: WbSettings (WB__*) ----------------------------------------------


def test_wb_settings_defaults_are_the_measured_numbers() -> None:
    """Literals of `PROJECT.md` section 2.12 Р12, not read from the model."""
    wb = WbSettings()

    assert wb.TOKEN_TIMEOUT_SECONDS == 30.0
    assert wb.FAILURE_PAUSE_SECONDS == 300.0
    assert wb.LAUNCH_BUDGET == 8
    assert wb.LAUNCH_WINDOW_SECONDS == 900.0
    assert wb.BROWSER_SANDBOX is False


_WB_FLOAT_BOUNDS = [
    ("TOKEN_TIMEOUT_SECONDS", 120),
    ("FAILURE_PAUSE_SECONDS", 3600),
    ("LAUNCH_WINDOW_SECONDS", 86400),
]


@pytest.mark.parametrize(("field", "upper"), _WB_FLOAT_BOUNDS)
def test_wb_float_field_accepts_smallest_positive_and_upper_bound(
    field: str, upper: int
) -> None:
    assert getattr(WbSettings.model_validate({field: 0.001}), field) == 0.001
    assert getattr(WbSettings.model_validate({field: upper}), field) == float(upper)


@pytest.mark.parametrize(("field", "upper"), _WB_FLOAT_BOUNDS)
@pytest.mark.parametrize(
    ("bad_offset", "error_type"),
    [(None, "greater_than"), (1, "less_than_equal")],
    ids=["zero", "above-upper"],
)
def test_wb_float_field_rejects_zero_and_above_the_upper_bound(
    field: str, upper: int, bad_offset: int | None, error_type: str
) -> None:
    value = 0 if bad_offset is None else upper + bad_offset

    with pytest.raises(ValidationError) as excinfo:
        WbSettings.model_validate({field: value})

    assert [(e["type"], e["loc"]) for e in excinfo.value.errors()] == [
        (error_type, (field,))
    ]


@pytest.mark.parametrize(("field", "upper"), _WB_FLOAT_BOUNDS)
def test_wb_float_field_rejects_negative_value(field: str, upper: int) -> None:
    with pytest.raises(ValidationError):
        WbSettings.model_validate({field: -1})


@pytest.mark.parametrize(("field", "upper"), _WB_FLOAT_BOUNDS)
@pytest.mark.parametrize(
    "value",
    [float("inf"), float("-inf"), float("nan"), "inf", "nan", "1e400"],
    ids=["inf", "-inf", "nan", "str-inf", "str-nan", "str-1e400"],
)
def test_wb_float_field_rejects_non_finite_values(
    field: str, upper: int, value: float | str
) -> None:
    """Agreement 12: ``gt``/``le`` alone do not reject infinity; ``'1e400'``
    overflows to ``inf`` inside ``float()``."""
    with pytest.raises(ValidationError) as excinfo:
        WbSettings.model_validate({field: value})

    assert [e["type"] for e in excinfo.value.errors()] == ["finite_number"]


@pytest.mark.parametrize(
    ("value", "accepted"),
    [(-1, False), (0, False), (1, True), (8, True), (100, True), (101, False)],
)
def test_wb_launch_budget_boundaries(value: int, accepted: bool) -> None:
    if accepted:
        assert value == WbSettings(LAUNCH_BUDGET=value).LAUNCH_BUDGET
        return
    with pytest.raises(ValidationError) as excinfo:
        WbSettings(LAUNCH_BUDGET=value)
    assert [e["loc"] for e in excinfo.value.errors()] == [("LAUNCH_BUDGET",)]


def test_wb_launch_budget_rejects_a_fractional_value() -> None:
    with pytest.raises(ValidationError):
        WbSettings(LAUNCH_BUDGET=1.5)  # type: ignore[arg-type]


def test_settings_without_wb_variables_uses_wb_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``WB__*`` anywhere: the section still exists with its defaults, so
    the bot starts with an old `.env`."""
    _delenv_settings_prefix_space(monkeypatch)

    settings = Settings(
        _env_file=None,
        db=_make_db_settings(),
        tg=_make_bot_secret(),
        redis=_make_redis_settings(host="h"),
    )

    assert settings.wb == WbSettings()
    assert settings.wb.LAUNCH_BUDGET == 8


def test_wb_environment_variables_override_one_field_and_keep_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``WB__LAUNCH_BUDGET=3`` (not the default 8) reaches the model through
    the real environment path; the other four stay at their defaults."""
    _delenv_settings_prefix_space(monkeypatch)
    monkeypatch.setenv("WB__LAUNCH_BUDGET", "3")
    monkeypatch.setenv("WB__BROWSER_SANDBOX", "true")

    settings = Settings(
        _env_file=None,
        db=_make_db_settings(),
        tg=_make_bot_secret(),
        redis=_make_redis_settings(host="h"),
    )

    assert settings.wb.LAUNCH_BUDGET == 3
    assert settings.wb.BROWSER_SANDBOX is True
    assert settings.wb.TOKEN_TIMEOUT_SECONDS == 30.0
    assert settings.wb.FAILURE_PAUSE_SECONDS == 300.0
    assert settings.wb.LAUNCH_WINDOW_SECONDS == 900.0


def test_wb_environment_value_out_of_bounds_fails_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _delenv_settings_prefix_space(monkeypatch)
    monkeypatch.setenv("WB__TOKEN_TIMEOUT_SECONDS", "1e400")

    with pytest.raises(ValidationError) as excinfo:
        Settings(
            _env_file=None,
            db=_make_db_settings(),
            tg=_make_bot_secret(),
            redis=_make_redis_settings(host="h"),
        )

    assert [e["loc"] for e in excinfo.value.errors()] == [
        ("wb", "TOKEN_TIMEOUT_SECONDS")
    ]


def test_environment_clearing_covers_the_wb_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agreement 19: a stray shell ``WB__LAUNCH_BUDGET=999`` (out of bounds)
    must not break ``get_settings()`` tests - the helper clears it. Without
    ``WB__`` in the cleared prefix space the build below raises."""
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv("WB__LAUNCH_BUDGET", "999")
    _set_env_for_get_settings(monkeypatch)

    settings = get_settings.__wrapped__()

    assert settings.wb.LAUNCH_BUDGET == 8


def test_env_example_lists_every_wb_variable_with_its_default() -> None:
    """AC9: `.env.example` names all five ``WB__*`` variables, and each
    example value parses to the model's default (the file documents the
    default, not an invented tuning)."""
    example = (Path(__file__).resolve().parents[3] / ".env.example").read_text()
    values = {
        line.split("=", 1)[0]: line.split("=", 1)[1].strip()
        for line in example.splitlines()
        if line.startswith("WB__")
    }

    assert sorted(values) == sorted(f"WB__{name}" for name in WbSettings.model_fields)
    parsed = WbSettings.model_validate(
        {k.removeprefix("WB__"): v for k, v in values.items()}
    )
    assert parsed == WbSettings()


# --- PAB-110: OzonSettings (OZON__*) -----------------------------------------


def test_ozon_settings_defaults_are_the_numbers_of_the_architecture() -> None:
    """Literals of `PROJECT.md` section 2.14 O8, not read from the model."""
    ozon = OzonSettings()

    assert ozon.TOKEN_TIMEOUT_SECONDS == 30.0
    assert ozon.FAILURE_PAUSE_SECONDS == 300.0
    assert ozon.LAUNCH_BUDGET == 8
    assert ozon.LAUNCH_WINDOW_SECONDS == 900.0
    assert ozon.BROWSER_SANDBOX is False


@pytest.mark.parametrize(("field", "upper"), _WB_FLOAT_BOUNDS)
def test_ozon_float_field_accepts_smallest_positive_and_upper_bound(
    field: str, upper: int
) -> None:
    assert getattr(OzonSettings.model_validate({field: 0.001}), field) == 0.001
    assert getattr(OzonSettings.model_validate({field: upper}), field) == float(upper)


@pytest.mark.parametrize(("field", "upper"), _WB_FLOAT_BOUNDS)
@pytest.mark.parametrize(
    ("bad_offset", "error_type"),
    [(None, "greater_than"), (1, "less_than_equal")],
    ids=["zero", "above-upper"],
)
def test_ozon_float_field_rejects_zero_and_above_the_upper_bound(
    field: str, upper: int, bad_offset: int | None, error_type: str
) -> None:
    value = 0 if bad_offset is None else upper + bad_offset

    with pytest.raises(ValidationError) as excinfo:
        OzonSettings.model_validate({field: value})

    assert [(e["type"], e["loc"]) for e in excinfo.value.errors()] == [
        (error_type, (field,))
    ]


@pytest.mark.parametrize(("field", "upper"), _WB_FLOAT_BOUNDS)
def test_ozon_float_field_rejects_negative_value(field: str, upper: int) -> None:
    with pytest.raises(ValidationError):
        OzonSettings.model_validate({field: -1})


@pytest.mark.parametrize(("field", "upper"), _WB_FLOAT_BOUNDS)
@pytest.mark.parametrize(
    "value",
    [float("inf"), float("-inf"), float("nan"), "inf", "nan", "1e400"],
    ids=["inf", "-inf", "nan", "str-inf", "str-nan", "str-1e400"],
)
def test_ozon_float_field_rejects_non_finite_values(
    field: str, upper: int, value: float | str
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        OzonSettings.model_validate({field: value})

    assert [e["type"] for e in excinfo.value.errors()] == ["finite_number"]


@pytest.mark.parametrize(
    ("value", "accepted"),
    [(-1, False), (0, False), (1, True), (8, True), (100, True), (101, False)],
)
def test_ozon_launch_budget_boundaries(value: int, accepted: bool) -> None:
    if accepted:
        assert value == OzonSettings(LAUNCH_BUDGET=value).LAUNCH_BUDGET
        return
    with pytest.raises(ValidationError) as excinfo:
        OzonSettings(LAUNCH_BUDGET=value)
    assert [e["loc"] for e in excinfo.value.errors()] == [("LAUNCH_BUDGET",)]


def test_settings_without_ozon_variables_uses_ozon_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``OZON__*`` anywhere: the bot starts with an old `.env`."""
    _delenv_settings_prefix_space(monkeypatch)

    settings = Settings(
        _env_file=None,
        db=_make_db_settings(),
        tg=_make_bot_secret(),
        redis=_make_redis_settings(host="h"),
    )

    assert settings.ozon == OzonSettings()
    assert settings.ozon.LAUNCH_BUDGET == 8


def test_ozon_environment_variables_override_one_field_and_keep_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _delenv_settings_prefix_space(monkeypatch)
    monkeypatch.setenv("OZON__LAUNCH_BUDGET", "3")
    monkeypatch.setenv("OZON__BROWSER_SANDBOX", "true")

    settings = Settings(
        _env_file=None,
        db=_make_db_settings(),
        tg=_make_bot_secret(),
        redis=_make_redis_settings(host="h"),
    )

    assert settings.ozon.LAUNCH_BUDGET == 3
    assert settings.ozon.BROWSER_SANDBOX is True
    assert settings.ozon.TOKEN_TIMEOUT_SECONDS == 30.0
    assert settings.ozon.FAILURE_PAUSE_SECONDS == 300.0
    assert settings.ozon.LAUNCH_WINDOW_SECONDS == 900.0
    assert settings.wb == WbSettings()  # the WB section is independent


def test_ozon_environment_value_out_of_bounds_fails_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _delenv_settings_prefix_space(monkeypatch)
    monkeypatch.setenv("OZON__TOKEN_TIMEOUT_SECONDS", "1e400")

    with pytest.raises(ValidationError) as excinfo:
        Settings(
            _env_file=None,
            db=_make_db_settings(),
            tg=_make_bot_secret(),
            redis=_make_redis_settings(host="h"),
        )

    assert [e["loc"] for e in excinfo.value.errors()] == [
        ("ozon", "TOKEN_TIMEOUT_SECONDS")
    ]


def test_environment_clearing_covers_the_ozon_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv("OZON__LAUNCH_BUDGET", "999")
    _set_env_for_get_settings(monkeypatch)

    settings = get_settings.__wrapped__()

    assert settings.ozon.LAUNCH_BUDGET == 8


def test_env_example_lists_every_ozon_variable_with_its_default() -> None:
    example = (Path(__file__).resolve().parents[3] / ".env.example").read_text()
    values = {
        line.split("=", 1)[0]: line.split("=", 1)[1].strip()
        for line in example.splitlines()
        if line.startswith("OZON__")
    }

    assert sorted(values) == sorted(
        f"OZON__{name}" for name in OzonSettings.model_fields
    )
    parsed = OzonSettings.model_validate(
        {k.removeprefix("OZON__"): v for k, v in values.items()}
    )
    assert parsed == OzonSettings()


# --- AppSettings.DISPLAY_TIMEZONE (PAB-100 AC10) ---------------------------


def test_display_timezone_defaults_to_europe_moscow() -> None:
    assert ZoneInfo("Europe/Moscow") == AppSettings().DISPLAY_TIMEZONE


def test_display_timezone_name_is_turned_into_a_zoneinfo() -> None:
    zone = AppSettings.model_validate({"DISPLAY_TIMEZONE": "Asia/Novosibirsk"})

    assert isinstance(zone.DISPLAY_TIMEZONE, ZoneInfo)
    assert zone.DISPLAY_TIMEZONE.key == "Asia/Novosibirsk"


def test_display_timezone_name_with_surrounding_spaces_is_accepted() -> None:
    zone = AppSettings.model_validate({"DISPLAY_TIMEZONE": "  Asia/Novosibirsk "})

    assert zone.DISPLAY_TIMEZONE.key == "Asia/Novosibirsk"


def test_display_timezone_accepts_a_ready_zoneinfo() -> None:
    zone = AppSettings.model_validate({"DISPLAY_TIMEZONE": ZoneInfo("Asia/Tokyo")})

    assert zone.DISPLAY_TIMEZONE.key == "Asia/Tokyo"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("Mars/Olympus", id="unknown-name"),
        pytest.param("", id="empty"),
        pytest.param("  ", id="blank"),
        pytest.param("../etc/passwd", id="path-traversal"),
        pytest.param(5, id="not-a-string"),
    ],
)
def test_display_timezone_rejects_unknown_empty_blank_and_non_string(
    value: object,
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        AppSettings.model_validate({"DISPLAY_TIMEZONE": value})

    assert [e["loc"] for e in excinfo.value.errors()] == [("DISPLAY_TIMEZONE",)]


def test_display_timezone_env_variable_overrides_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _delenv_settings_prefix_space(monkeypatch)
    monkeypatch.setenv("APP__DISPLAY_TIMEZONE", "Asia/Novosibirsk")

    settings = Settings(
        _env_file=None,
        db=_make_db_settings(),
        tg=_make_bot_secret(),
        redis=_make_redis_settings(host="h"),
    )

    assert ZoneInfo("Asia/Novosibirsk") == settings.app.DISPLAY_TIMEZONE


def test_display_timezone_env_variable_with_unknown_name_fails_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _delenv_settings_prefix_space(monkeypatch)
    monkeypatch.setenv("APP__DISPLAY_TIMEZONE", "Mars/Olympus")

    with pytest.raises(ValidationError) as excinfo:
        Settings(
            _env_file=None,
            db=_make_db_settings(),
            tg=_make_bot_secret(),
            redis=_make_redis_settings(host="h"),
        )

    assert [e["loc"] for e in excinfo.value.errors()] == [("app", "DISPLAY_TIMEZONE")]


def test_environment_clearing_covers_the_display_timezone_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stray shell ``APP__DISPLAY_TIMEZONE`` must not leak into the helper's
    tests: the prefix-space clearing removes it."""
    monkeypatch.setenv("APP__DISPLAY_TIMEZONE", "Mars/Olympus")
    _delenv_settings_prefix_space(monkeypatch)

    settings = Settings(
        _env_file=None,
        db=_make_db_settings(),
        tg=_make_bot_secret(),
        redis=_make_redis_settings(host="h"),
    )

    assert ZoneInfo("Europe/Moscow") == settings.app.DISPLAY_TIMEZONE


def test_env_example_documents_the_display_timezone_default() -> None:
    example = (Path(__file__).resolve().parents[3] / ".env.example").read_text()
    values = [
        line.split("=", 1)[1].strip()
        for line in example.splitlines()
        if line.startswith("APP__DISPLAY_TIMEZONE=")
    ]

    assert values == ["Europe/Moscow"]
    assert (
        AppSettings.model_validate({"DISPLAY_TIMEZONE": values[0]}).DISPLAY_TIMEZONE
        == AppSettings().DISPLAY_TIMEZONE
    )


@pytest.mark.parametrize("path", ["", " ", ":memory:", "/"])
def test_sqlite_database_path_rejects_non_file_values(path: str) -> None:
    with pytest.raises(ValidationError):
        DatabaseSettings.model_validate({"PATH": path})


@pytest.mark.parametrize("value", [0, -1, 301, float("inf"), float("nan")])
def test_sqlite_busy_timeout_rejects_unbounded_or_nonfinite_values(
    value: float,
) -> None:
    with pytest.raises(ValidationError):
        DatabaseSettings(BUSY_TIMEOUT_SECONDS=value)


@pytest.mark.parametrize("value", [0.01, 300.0])
def test_sqlite_busy_timeout_accepts_finite_bounds(value: float) -> None:
    assert value == DatabaseSettings(BUSY_TIMEOUT_SECONDS=value).BUSY_TIMEOUT_SECONDS
