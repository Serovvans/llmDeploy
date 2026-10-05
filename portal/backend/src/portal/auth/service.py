"""Сценарии входа: пароль, шаги входа, второй фактор, сессии (docs/portal-api.md §2)."""

import asyncio
import hmac
import secrets
from concurrent.futures import Executor
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from portal.auth import errors
from portal.auth.domain import (
    TOTP_PERIOD_SECONDS,
    SecondFactorSetup,
    Session,
    SessionState,
    User,
    login_step,
    login_throttle_key,
    token_hash,
)
from portal.auth.ports import (
    AuthUnitOfWork,
    AuthUnitOfWorkFactory,
    PasswordHasher,
    SecretCipher,
    TotpProvider,
)
from portal.auth.throttle import Throttle
from portal.core.errors import (
    AppError,
    field_error,
    login_step_expired,
    login_step_required,
    unauthenticated,
    validation_error,
)
from portal.core.ports import Clock, LoginStep, SessionInfo, SessionMissingError
from portal.core.settings import AuthSettings

BACKUP_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_BACKUP_CODE_LENGTH = 8
_TOTP_DIGITS = 6
_TOKEN_BYTES = 32
_TOUCH_INTERVAL = timedelta(minutes=1)


@dataclass(frozen=True)
class SecondFactorResult:
    """Итог ввода кода на шаге `second_factor`."""

    state: SessionState
    backup_code_used: bool


@dataclass(frozen=True)
class ConfirmResult:
    """Итог подтверждения настройки: резервные коды показываются один раз."""

    state: SessionState
    backup_codes: list[str]


def normalize_backup_code(code: str) -> str:
    """Привести резервный код к виду хранения: без пробелов и дефисов, верхний регистр."""
    return code.replace(" ", "").replace("-", "").upper()


def _new_backup_code() -> str:
    return "".join(secrets.choice(BACKUP_CODE_ALPHABET) for _ in range(_BACKUP_CODE_LENGTH))


class AuthService:
    """Вход по паролю, шаги входа и проверка сессии."""

    def __init__(
        self,
        uow_factory: AuthUnitOfWorkFactory,
        hasher: PasswordHasher,
        hash_executor: Executor,
        cipher: SecretCipher,
        totp: TotpProvider,
        clock: Clock,
        settings: AuthSettings,
        common_passwords: frozenset[str],
    ) -> None:
        """Получить зависимости явно; заглушечный хеш считается один раз.

        `hash_executor` — свои потоки под Argon2: общий пул цикла событий бывает целиком
        занят разбором файлов, и вход не должен ждать его в очереди.
        """
        self._uow_factory = uow_factory
        self._hasher = hasher
        self._hash_executor = hash_executor
        self._cipher = cipher
        self._totp = totp
        self._clock = clock
        self._settings = settings
        self._common_passwords = common_passwords
        # Для неизвестного логина проверяется этот хеш: время ответа то же, что при неверном пароле.
        self._dummy_hash = hasher.hash(secrets.token_urlsafe(_TOKEN_BYTES))

    # --- проверка сессии ---

    async def authenticate(self, token: str | None) -> SessionInfo:
        """Найти действующую сессию по значению cookie (порт `SessionAuthenticator`)."""
        if not token:
            raise SessionMissingError
        now = self._clock.now()
        async with self._uow_factory() as uow:
            found = await uow.sessions.by_token_hash(token_hash(token))
            if found is None:
                raise SessionMissingError
            session, user = found
            step = login_step(user, session)
            if self._is_expired(session, now) or user.is_blocked:
                await uow.sessions.delete(session.id)
                await uow.commit()
                raise SessionMissingError(step)
            if now - session.last_seen_at >= _TOUCH_INTERVAL:
                session.last_seen_at = now
                await uow.sessions.update(session, "last_seen_at")
            return SessionInfo(session.id, user.id, user.role, user.full_name, step)

    async def session_exists(self, session_id: UUID) -> bool:
        """Есть ли ещё сессия в базе (порт `SessionAuthenticator`)."""
        async with self._uow_factory() as uow:
            return await uow.sessions.exists(session_id)

    async def session_state(self, session_id: UUID) -> SessionState:
        """Объект `Session` для `GET /api/auth/session`."""
        async with self._uow_factory() as uow:
            session, user = await self._load(uow, session_id)
            return await self._state(uow, user, session)

    async def logout(self, session_id: UUID) -> None:
        """Удалить сессию."""
        async with self._uow_factory() as uow:
            await uow.sessions.delete(session_id)

    # --- вход ---

    async def login(self, login: str, password: str, ip: str | None) -> SessionState:
        """Проверить логин и пароль и создать сессию незавершённого входа."""
        login = login.lower()
        key = login_throttle_key(login)
        now = self._clock.now()
        async with self._uow_factory() as uow:
            throttle = Throttle(uow.throttle, self._settings, now)
            candidate = await uow.users.by_login(login)

            # Отказы по блокировке и по лимиту адреса в аудит не пишутся и ничего не
            # считают (§2.5): иначе журнал рос бы от дешёвых запросов.
            if (wait := await throttle.ip_blocked_for(ip)) is not None:
                raise errors.too_many_attempts(wait)
            await uow.throttle.lock_login(key)
            if (wait := await throttle.login_locked_for(key)) is not None:
                raise errors.login_locked(wait)

            password_hash = candidate.password_hash if candidate else self._dummy_hash
            password_ok = await self._password_matches(password_hash, password)
            # Проверка пароля долгая: за это время учётную запись могли заблокировать или
            # сбросить ей пароль. Решение принимается по строке, перечитанной под
            # блокировкой: администратор либо уже записал своё, либо дождётся этого входа
            # и погасит его сессию.
            user = await uow.users.by_login(login, lock=True) if password_ok else None
            if user is None or user.password_hash != password_hash:
                lock_seconds = await throttle.record_failure(key, ip)
                await self._audit_failure(uow, candidate, ip, "invalid_credentials", lock_seconds)
                await uow.commit()
                raise errors.invalid_credentials()
            if user.is_blocked:
                await throttle.record_ip_failure(ip)
                await self._audit_failure(uow, user, ip, "account_blocked")
                await uow.commit()
                raise errors.account_blocked()

            await uow.sessions.delete_expired(user.id, now, now - self._idle_ttl)
            if not user.totp_enabled:
                await throttle.reset_login(key)
            token = secrets.token_urlsafe(_TOKEN_BYTES)
            session = Session(
                id=uuid4(),
                user_id=user.id,
                token_hash=token_hash(token),
                second_factor_passed=False,
                created_at=now,
                last_seen_at=now,
                expires_at=now,
            )
            return await self._enter_step(uow, user, session, ip, now, new_token=token)

    async def second_factor(
        self, session_id: UUID, code: str | None, backup_code: str | None, ip: str | None
    ) -> SecondFactorResult:
        """Принять код из приложения или резервный код на шаге `second_factor`."""
        now = self._clock.now()
        async with self._uow_factory() as uow:
            _, user = await self._load(uow, session_id, "second_factor")
            key = login_throttle_key(user.login)
            throttle = Throttle(uow.throttle, self._settings, now)

            if (wait := await throttle.ip_blocked_for(ip)) is not None:
                raise errors.too_many_attempts(wait)
            await uow.throttle.lock_login(key)
            # Под блокировкой логина данные читаются заново: параллельная попытка могла
            # уже принять этот код или закрыть вход.
            session, user = await self._load(uow, session_id, "second_factor", lock=True)
            if (wait := await throttle.login_locked_for(key)) is not None:
                await uow.sessions.delete(session.id)
                await uow.commit()
                raise errors.login_locked(wait)

            if backup_code is not None:
                hmac_value = self._cipher.backup_code_hmac(normalize_backup_code(backup_code))
                accepted = await uow.backup_codes.use(user.id, hmac_value, now)
                failure = None if accepted else errors.invalid_backup_code()
                changed: tuple[str, ...] = ()
            else:
                failure = self._accept_totp(user, code or "", now)
                changed = ("totp_last_step",)
            if failure is not None:
                lock_seconds = await throttle.record_failure(key, ip)
                if lock_seconds is not None:
                    await uow.sessions.delete(session.id)
                # Причина в журнале совпадает с кодом ответа (§9.5).
                await self._audit_failure(uow, user, ip, failure.code, lock_seconds)
                await uow.commit()
                if lock_seconds is not None:
                    # Сессия шага удалена: клиенту нужен ответ о блокировке, а не о коде,
                    # иначе следующая попытка выглядела бы как истёкшее время на ввод.
                    raise errors.login_locked(lock_seconds)
                raise failure

            session.second_factor_passed = True
            await throttle.reset_login(key)
            state = await self._enter_step(uow, user, session, ip, now, changed=changed)
            return SecondFactorResult(state, backup_code_used=backup_code is not None)

    # --- пароль ---

    async def change_password(
        self, session_id: UUID, new_password: str, current_password: str | None, ip: str | None
    ) -> SessionState | None:
        """Сменить пароль; на шаге `password_change` вернуть новую сессию, на `ready` — ничего."""
        now = self._clock.now()
        async with self._uow_factory() as uow:
            session, user = await self._load(uow, session_id, "password_change", "ready")
            step = login_step(user, session)
            voluntary = step == "ready"
            if voluntary:
                await self._check_current_password(uow, user, current_password, ip, now)
            await self._check_new_password(user, new_password)
            checked_hash = user.password_hash
            new_hash = await asyncio.get_running_loop().run_in_executor(
                self._hash_executor, self._hasher.hash, new_password
            )

            # Проверки и хеширование долгие. Сброс пароля, блокировка и смена роли гасят
            # сессии; если под блокировкой строки сессия на месте и на том же шаге, ничего
            # из этого не произошло, иначе смена пароля отменяется.
            found = await uow.sessions.get(session_id, lock_user=True)
            if (
                found is None
                or login_step(found[1], found[0]) != step
                or found[1].password_hash != checked_hash
            ):
                raise unauthenticated() if voluntary else login_step_expired()
            session, user = found
            user.password_hash = new_hash
            user.must_change_password = False
            changed = ("password_hash", "must_change_password")
            await uow.sessions.delete_for_user(user.id)
            await uow.audit.record(
                "password_changed",
                actor_id=user.id,
                subject_user_id=user.id,
                ip=ip,
                details={"forced": not voluntary},
            )
            if voluntary:
                await uow.users.update(user, *changed)
                return None
            token = secrets.token_urlsafe(_TOKEN_BYTES)
            successor = Session(
                id=uuid4(),
                user_id=user.id,
                token_hash=token_hash(token),
                second_factor_passed=session.second_factor_passed,
                created_at=session.created_at,
                last_seen_at=now,
                expires_at=session.expires_at,
            )
            return await self._enter_step(
                uow, user, successor, ip, now, new_token=token, changed=changed
            )

    # --- настройка второго фактора ---

    async def start_second_factor_setup(self, session_id: UUID) -> SecondFactorSetup:
        """Выдать ключ и QR-код; до подтверждения повторный вызов возвращает тот же ключ."""
        async with self._uow_factory() as uow:
            _, user = await self._load(uow, session_id, "second_factor_setup", lock=True)
            if user.totp_secret is None:
                secret = self._totp.new_secret()
                user.totp_secret = self._cipher.encrypt(secret.encode())
                await uow.users.update(user, "totp_secret")
            else:
                secret = self._cipher.decrypt(user.totp_secret).decode()
            size, path = self._totp.provisioning_qr(secret, user.login)
            return SecondFactorSetup(secret=secret, qr_size=size, qr_path=path)

    async def confirm_second_factor_setup(
        self, session_id: UUID, code: str, ip: str | None
    ) -> ConfirmResult:
        """Включить второй фактор по коду и один раз выдать резервные коды."""
        now = self._clock.now()
        async with self._uow_factory() as uow:
            session, user = await self._load(uow, session_id, "second_factor_setup", lock=True)
            if user.totp_secret is None:
                raise errors.setup_not_started()
            if (failure := self._accept_totp(user, code, now)) is not None:
                raise failure
            user.totp_enabled = True
            session.second_factor_passed = True
            codes = [_new_backup_code() for _ in range(self._settings.totp.backup_codes_count)]
            await uow.backup_codes.replace(
                user.id, [self._cipher.backup_code_hmac(value) for value in codes]
            )
            state = await self._enter_step(
                uow, user, session, ip, now, changed=("totp_enabled", "totp_last_step")
            )
            return ConfirmResult(state, [f"{value[:4]}-{value[4:]}" for value in codes])

    # --- общее ---

    @property
    def _idle_ttl(self) -> timedelta:
        return timedelta(minutes=self._settings.session.idle_ttl_minutes)

    def _is_expired(self, session: Session, now: datetime) -> bool:
        return now >= session.expires_at or now - session.last_seen_at > self._idle_ttl

    async def _load(
        self, uow: AuthUnitOfWork, session_id: UUID, *allowed_steps: LoginStep, lock: bool = False
    ) -> tuple[Session, User]:
        """Сессия и пользователь; шаг сверяется повторно — он мог смениться после проверки.

        `lock` держит строку пользователя до конца транзакции: запросы одного пользователя
        и действия администратора над ним идут по очереди.
        """
        found = await uow.sessions.get(session_id, lock_user=lock)
        if found is None:
            raise login_step_expired()
        session, user = found
        step = login_step(user, session)
        if allowed_steps and step not in allowed_steps:
            raise login_step_required(step)
        return session, user

    async def _state(
        self, uow: AuthUnitOfWork, user: User, session: Session, new_token: str | None = None
    ) -> SessionState:
        step = login_step(user, session)
        if step == "second_factor":
            return SessionState(step, None, None, session.expires_at, new_token)
        codes = await uow.backup_codes.counts(user.id) if user.totp_enabled else None
        return SessionState(step, user, codes, session.expires_at, new_token)

    async def _enter_step(
        self,
        uow: AuthUnitOfWork,
        user: User,
        session: Session,
        ip: str | None,
        now: datetime,
        new_token: str | None = None,
        changed: tuple[str, ...] = (),
    ) -> SessionState:
        """Сохранить сессию и пользователя после перехода между шагами входа.

        Срок сессии зависит от шага (§2.6); переход в `ready` завершает вход. У
        пользователя пишутся только поля из `changed` и отметка завершённого входа.
        """
        session_settings = self._settings.session
        if login_step(user, session) == "ready":
            session.expires_at = session.created_at + timedelta(
                hours=session_settings.absolute_ttl_hours
            )
            user.last_login_at = now
            changed = (*changed, "last_login_at")
            await uow.audit.record(
                "login_succeeded", actor_id=user.id, subject_user_id=user.id, ip=ip
            )
        else:
            session.expires_at = session.created_at + timedelta(
                minutes=session_settings.login_step_ttl_minutes
            )
        if changed:
            await uow.users.update(user, *changed)
        if new_token is None:
            await uow.sessions.update(session, "second_factor_passed", "expires_at")
        else:
            await uow.sessions.add(session)
        return await self._state(uow, user, session, new_token)

    @staticmethod
    async def _audit_failure(
        uow: AuthUnitOfWork,
        user: User | None,
        ip: str | None,
        reason: str,
        lock_seconds: int | None = None,
    ) -> None:
        """Записать неудачу; `lock_seconds` — эта попытка вызвала блокировку логина."""
        details: dict[str, object] = {"reason": reason}
        if lock_seconds is not None:
            details["lock_seconds"] = lock_seconds
        await uow.audit.record(
            "login_failed", subject_user_id=user.id if user else None, ip=ip, details=details
        )

    def _accept_totp(self, user: User, code: str, now: datetime) -> AppError | None:
        """Принять код: запомнить его шаг у пользователя либо вернуть причину отказа."""
        if user.totp_secret is None:
            return errors.invalid_code()
        code = code.replace(" ", "")
        if len(code) != _TOTP_DIGITS or not code.isdecimal():
            return errors.invalid_code()
        secret = self._cipher.decrypt(user.totp_secret).decode()
        current = int(now.timestamp()) // TOTP_PERIOD_SECONDS
        window = self._settings.totp.window_steps
        matched = [
            step
            for step in range(current - window, current + window + 1)
            if hmac.compare_digest(self._totp.code_at(secret, step), code)
        ]
        if not matched:
            return errors.invalid_code()
        fresh = [
            step for step in matched if user.totp_last_step is None or step > user.totp_last_step
        ]
        if not fresh:
            return errors.code_already_used()
        user.totp_last_step = max(fresh)
        return None

    async def _password_matches(self, password_hash: str, password: str) -> bool:
        """Проверить пароль; строка длиннее предела не хешируется и считается неверной."""
        if len(password) > self._settings.password.max_length:
            return False
        return await asyncio.get_running_loop().run_in_executor(
            self._hash_executor, self._hasher.verify, password_hash, password
        )

    async def _check_current_password(
        self,
        uow: AuthUnitOfWork,
        user: User,
        current_password: str | None,
        ip: str | None,
        now: datetime,
    ) -> None:
        """Текущий пароль при добровольной смене: неверный — неудача по логину (§2.5)."""
        if current_password is None:
            raise validation_error([field_error("current_password", "required")])
        key = login_throttle_key(user.login)
        throttle = Throttle(uow.throttle, self._settings, now)
        await uow.throttle.lock_login(key)
        if (wait := await throttle.login_locked_for(key)) is not None:
            raise errors.login_locked(wait)
        if not await self._password_matches(user.password_hash, current_password):
            lock_seconds = await throttle.record_failure(key, ip)
            await self._audit_failure(uow, user, ip, "invalid_current_password", lock_seconds)
            await uow.commit()
            if lock_seconds is not None:
                # Попытка сама вызвала блокировку: ответ о ней, сессия остаётся (§2.5).
                raise errors.login_locked(lock_seconds)
            raise validation_error(
                [field_error("current_password", "current_password_invalid", "Неверный пароль.")]
            )

    async def _check_new_password(self, user: User, new_password: str) -> None:
        policy = self._settings.password
        if len(new_password) > policy.max_length:
            error = field_error("new_password", "too_long")
        elif len(new_password) < policy.min_length:
            error = field_error(
                "new_password",
                "password_too_short",
                f"Пароль короче {policy.min_length} символов.",
            )
        elif new_password.lower() in self._common_passwords:
            error = field_error(
                "new_password", "password_too_common", "Этот пароль слишком распространён."
            )
        elif await self._password_matches(user.password_hash, new_password):
            error = field_error(
                "new_password", "password_same_as_old", "Новый пароль совпадает с прежним."
            )
        else:
            return
        raise validation_error([error])
