"""Short-lived signed web sessions; no passwords are stored in cookies."""
import hashlib
import hmac
import secrets
import time

COOKIE_NAME = 'contract_review_session'
SESSION_SECONDS = 8 * 60 * 60
_PROCESS_KEY = secrets.token_bytes(32)


def credentials_match(user, password, expected_user, expected_password):
    return (secrets.compare_digest(user.encode(), expected_user.encode())
            and secrets.compare_digest(password.encode(), expected_password.encode()))


def _signature(payload, user, password):
    message = (user + '\0' + password + '\0' + payload).encode()
    return hmac.new(_PROCESS_KEY, message, hashlib.sha256).hexdigest()


def issue_session(user, password):
    payload = f'{int(time.time()) + SESSION_SECONDS}.{secrets.token_hex(16)}'
    return payload + '.' + _signature(payload, user, password)


def valid_session(token, user, password):
    if not token or len(token) > 160:
        return False
    try:
        expires, nonce, signature = token.split('.')
        remaining = int(expires) - time.time()
        return (0 < remaining <= SESSION_SECONDS and len(nonce) == 32
                and hmac.compare_digest(signature.encode(), _signature(expires + '.' + nonce, user, password).encode()))
    except (ValueError, TypeError, UnicodeError):
        return False
