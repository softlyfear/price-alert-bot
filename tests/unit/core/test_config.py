"""Tests for app.core.config.

Pairs with PAB-004 (RedisSettings, AppSettings), PAB-052 (SecretStr fields,
hide_input_in_errors) and, for DatabaseSettings.DATABASE_URL, code that has
existed unchanged and uncovered since the start of the project.

Isolation. Every ``Settings`` built here passes already-constructed nested
model *instances* (``DatabaseSettings``, ``BotSecret``, ``RedisSettings``,
``AppSettings``) as keyword arguments, plus ``_env_file=None``.
``pydantic_settings`` merges its sources with ``deep_update``
(``pydantic._internal._utils.deep_update``), which only recurses into a
value when it is a ``dict`` on *both* sides; a model instance is never a
``dict``, so an instance passed via kwargs always wins wholesale over
whatever ``os.environ`` or a dotenv file would have produced for that key.
That makes construction deterministic regardless of the host machine's
environment, without touching ``os.environ`` or the filesystem. Verified
empirically before writing these tests (env var set to a throwaway value,
instance-based construction ignored it; ``_env_file=None`` skips dotenv
entirely).

Two tests deliberately break this pattern by passing a raw, partial ``dict``
instead of an instance, to reach validation states that only exist before a
value is coerced into a model (a missing key, a field of the wrong type).
Because a raw dict *can* legitimately merge with whatever ``os.environ``
supplies for the same section, those two tests explicitly clear the one
environment variable that could interfere via ``monkeypatch.delenv(...,
raising=False)`` -- restored automatically by pytest, never written back.

Two tests exercise ``get_settings()`` itself (and, for one of them,
``get_settings()``'s own caching), which calls ``Settings()`` with no
overrides at all and therefore must read from the environment by
construction -- there is no instance-passing alternative for that call.
Both supply every required variable via ``monkeypatch.setenv`` (restored
automatically) and, because they cannot pass ``_env_file=None`` the way
every other test in this file does, both disable dotenv lookup for their
own duration via ``monkeypatch.setitem(Settings.model_config, "env_file",
None)`` -- also restored automatically once the test ends. Without this,
whatever real ``.env`` file happens to exist on the machine running the
suite would participate in the merge alongside the monkeypatched
variables, and the test's outcome would depend on machine state instead of
on the code; this is not hypothetical, since ``pydantic-settings`` treats
an explicitly-set environment variable and a value from ``.env`` as two
independent sources that get merged, the environment variable winning only
per-key. The first of the two calls ``get_settings.__wrapped__()`` to
bypass ``@lru_cache`` entirely, so the cached instance does not leak into
any other test sharing this process; the second calls ``get_settings()``
itself specifically to observe that caching, and clears the cache both
before and after via ``get_settings.cache_clear()`` so it neither reads a
stale instance left by another test nor leaves one behind for the next.

No test in this file writes to ``os.environ`` outside of ``monkeypatch``,
touches the filesystem, or reads an actual ``.env`` file: this holds
regardless of whether a ``.env`` exists on the machine running the suite,
because every ``Settings()`` construction in this file either passes
``_env_file=None`` directly or, for the two calls into ``get_settings()``,
neutralises the same dotenv source through ``Settings.model_config`` for
the duration of the test.
"""

from urllib.parse import unquote
from urllib.parse import urlsplit

import pytest
from pydantic import SecretStr
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from app.core.config import AppSettings
from app.core.config import BotSecret
from app.core.config import DatabaseSettings
from app.core.config import RedisSettings
from app.core.config import Settings
from app.core.config import get_settings

# A password containing every special character named by the ticket
# (`@ : / # % ? & + =`), a literal space, and Cyrillic, used everywhere a
# test needs to prove percent-encoding survives a roundtrip rather than
# just "looking fine" on a simple `pass123`-style password.
_SPECIAL_CHARS_PASSWORD = "p@ss/w:o#r%d?a&b+c= пароль"


def _make_db_settings(
    *, password: str = "db-marker-pass", host: str = "db-host"
) -> DatabaseSettings:
    """Build a DatabaseSettings with realistic, non-secret-looking values.

    Kept as a factory so scenario tests only override the field(s) they
    actually vary, instead of repeating twelve unrelated arguments.

    ``password`` is passed as a plain ``str`` and left for pydantic itself
    to coerce into ``SecretStr``, rather than wrapped in ``SecretStr(...)``
    here. This matters for the leak guards below: if ``PASSWORD`` were ever
    changed back to a plain ``str`` field, a marker pre-wrapped in
    ``SecretStr`` here would make pydantic reject it as the wrong input
    type, and the guard would fail at this construction call instead of at
    the ``repr``/``str``/``model_dump`` assertion it exists to protect.
    """
    return DatabaseSettings(
        USER="db_user",
        PASSWORD=password,  # type: ignore[arg-type]
        HOST=host,
        PORT=5432,
        NAME="pricebot",
        ECHO=False,
        POOL_SIZE=5,
        MAX_OVERFLOW=10,
        POOL_PRE_PING=True,
        POOL_RECYCLE=1800,
        AUTOFLUSH=False,
        EXPIRE_ON_COMMIT=False,
    )


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
    """Measured, frozen behaviour (agreement 15): the validator rejects only
    a host whose ``strip()`` is empty. ``' host '`` has real content once
    stripped, so it is accepted, and the value is kept exactly as given --
    no normalisation happens. This is not a requirement to preserve future
    behaviour, only a record of what the code does today.
    """
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
        AppSettings(**{field_name: 0})
    errors = excinfo.value.errors()
    assert any(
        e["type"] == "greater_than" and e["loc"] == (field_name,) for e in errors
    )

    with pytest.raises(ValidationError) as excinfo:
        AppSettings(**{field_name: -1})
    errors = excinfo.value.errors()
    assert any(
        e["type"] == "greater_than" and e["loc"] == (field_name,) for e in errors
    )

    assert getattr(AppSettings(**{field_name: 1}), field_name) == 1


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
    """Catches a downgrade from ``quote(password, safe="")`` to
    ``quote(password)`` (default ``safe="/"``): with the default, the ``/``
    in the password would end up unescaped in the URL, which breaks netloc
    parsing (the password would no longer round-trip through a standard URL
    parser), not just "look different" the way it would on a plain
    ``pass123``.
    """
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


def test_database_url_roundtrips_special_character_password_via_sqlalchemy() -> None:
    db = _make_db_settings(password=_SPECIAL_CHARS_PASSWORD, host="db-host")

    parsed = make_url(db.DATABASE_URL)

    assert parsed.drivername == "postgresql+asyncpg"
    assert parsed.username == "db_user"
    assert parsed.password == _SPECIAL_CHARS_PASSWORD
    assert parsed.host == "db-host"
    assert parsed.port == 5432
    assert parsed.database == "pricebot"


# --- Secrets never leak: repr / str / model_dump / model_dump_json --------


def test_database_password_secret_not_leaked_in_repr_str_and_dumps() -> None:
    marker = "db-marker-do-not-leak"
    db = _make_db_settings(password=marker)

    assert marker not in repr(db)
    assert marker not in str(db)
    assert marker not in str(db.model_dump())
    assert marker not in db.model_dump_json()


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
    """Point 1b: built through ``Settings``, not ``RedisSettings`` directly.

    ``hide_input_in_errors`` only takes effect on the model whose
    ``validate_python`` is actually invoked -- measured at PAB-052
    acceptance (agreement 14): set on ``Settings`` it hides the raw input,
    set on ``RedisSettings`` directly it does nothing.
    ``RedisSettings(HOST=...)`` constructed directly still prints
    ``input_value=...`` in its ``ValidationError``. A guard built that way
    would be red even with the protection fully working, and the obvious
    "fix" would be to weaken the protection instead of the test -- which is
    exactly what this ticket exists to prevent.

    The raw ``dict`` passed for ``redis`` (instead of a ``RedisSettings``
    instance) is what lets pydantic hit the pre-coercion ``type=missing``
    path where the leak actually happens; a fully-built instance never
    reaches that path. Because a raw dict can still be deep-merged with
    ``os.environ`` for keys it does not supply, ``REDIS__HOST`` is cleared
    defensively so the test's premise (HOST absent) holds regardless of the
    host machine's environment.
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
    """Point 1c: documents the accepted side effect of ``hide_input_in_errors``
    -- it hides the raw input for *any* field on ``Settings``, not only
    secrets. ``DB__PORT`` is not a secret, but a bad value for it (a string
    that cannot parse as an integer) still must not echo the raw string in
    the error text. This locks in the documented cost (agreement 14) as an
    independent guard: removing ``hide_input_in_errors`` breaks this test
    too, not only the password-specific one above.
    """
    monkeypatch.delenv("DB__PORT", raising=False)
    bad_port_marker = "not-an-integer-marker"
    malformed_db = {
        "USER": "db_user",
        "PASSWORD": SecretStr("db-marker-pass"),
        "HOST": "db-host",
        "PORT": bad_port_marker,
        "NAME": "pricebot",
        "ECHO": False,
        "POOL_SIZE": 5,
        "MAX_OVERFLOW": 10,
        "POOL_PRE_PING": True,
        "POOL_RECYCLE": 1800,
        "AUTOFLUSH": False,
        "EXPIRE_ON_COMMIT": False,
    }
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
        e["type"] == "int_parsing" and e["loc"] == ("db", "PORT") for e in errors
    )
    assert bad_port_marker not in str(excinfo.value)


# --- APP__* partial override: default_factory must not suppress env reads -


def test_app_settings_env_var_partial_override_keeps_other_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sets exactly one ``APP__*`` variable and expects the other four to
    fall back to ``AppSettings``' own field defaults. The value (7) is
    deliberately different from both the field default (50) and the value
    in ``.env.example`` (also 50) -- if ``app: AppSettings =
    Field(default_factory=AppSettings)`` ever stopped reading the merged
    environment dict at all (e.g. because ``default_factory`` fired instead
    of validating the partial section), a test using a value equal to the
    default would stay green while the section became inert. The other four
    ``APP__*`` variables are cleared defensively so ambient shell state
    cannot leak in.
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


def _set_env_for_get_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set every environment variable ``get_settings()`` needs to build a
    valid ``Settings`` starting from a completely empty environment.

    Shared by the two tests below that exercise ``get_settings()`` itself
    -- the one call site in this file that must read the environment by
    construction, since it calls ``Settings()`` with no overrides at all.
    """
    monkeypatch.setenv("DB__USER", "db_user")
    monkeypatch.setenv("DB__PASSWORD", "db-marker-pass")
    monkeypatch.setenv("DB__HOST", "db-host")
    monkeypatch.setenv("DB__PORT", "5432")
    monkeypatch.setenv("DB__NAME", "pricebot")
    monkeypatch.setenv("DB__ECHO", "false")
    monkeypatch.setenv("DB__POOL_SIZE", "5")
    monkeypatch.setenv("DB__MAX_OVERFLOW", "10")
    monkeypatch.setenv("DB__POOL_PRE_PING", "true")
    monkeypatch.setenv("DB__POOL_RECYCLE", "1800")
    monkeypatch.setenv("DB__AUTOFLUSH", "false")
    monkeypatch.setenv("DB__EXPIRE_ON_COMMIT", "false")
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

    assert settings.db.HOST == "db-host"
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
    instead of by accident matching some other section's value. No other
    test in this file inspects more than one section at a time, so none of
    them would catch a swap like that.
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

    assert settings.db.HOST == "explicit-db-host"
    assert settings.tg.BOT_TOKEN.get_secret_value() == "explicit-bot-token"
    assert settings.redis.HOST == "explicit-redis-host"
    assert settings.app.SCHEDULER_INTERVAL_SECONDS == 111
    assert settings.app.ALERT_COOLDOWN_SECONDS == 222
    assert settings.app.MAX_PRODUCTS_PER_USER == 33
    assert settings.app.MARKETPLACE_CONCURRENCY == 4
    assert settings.app.HTTP_TIMEOUT_SECONDS == 12.5
