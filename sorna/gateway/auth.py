from datetime import datetime, timedelta
import functools
import hashlib, hmac
import logging

from aiohttp import web
from dateutil.tz import tzutc
from dateutil.parser import parse as dtparse
import simplejson as json
import sqlalchemy as sa

from sorna.exceptions import InvalidAuthParameters, AuthorizationFailed
from .models import User, KeyPair

log = logging.getLogger('sorna.gateway.auth')


def _extract_auth_params(request):
    """
    HTTP Authorization header must be formatted as:
    "Authorization: Sorna signMethod=HMAC-SHA256, credential=<ACCESS_KEY>:<SIGNATURE>"
    """
    auth_hdr = request.headers.get('Authorization')
    if not auth_hdr:
        return None
    pieces = auth_hdr.split(' ', 1)
    if len(pieces) != 2:
        return None
    auth_type, auth_str = pieces
    if auth_type != 'Sorna':
        return None

    raw_params = map(lambda s: s.strip(), auth_str.split(','))
    params = {}
    for param in raw_params:
        key, value = param.split('=', 1)
        params[key.strip()] = value.strip()

    try:
        access_key, signature = params['credential'].split(':', 1)
        ret = params['signMethod'], access_key, signature
        return ret
    except (KeyError, ValueError):
        return None


def check_date(request) -> bool:
    date = request.headers.get('Date')
    if not date:
        date = request.headers.get('X-Sorna-Date')
    if not date:
        return False
    try:
        dt = dtparse(date)
        request.date = dt
        now = datetime.now(tzutc())
        min_time = now - timedelta(minutes=15)
        max_time = now + timedelta(minutes=15)
        if dt < min_time or dt > max_time:
            return False
    except ValueError:
        return False
    return True


async def sign_request(sign_method, request, secret_key) -> str:
    try:
        assert hasattr(request, 'date')
        mac_type, hash_type = map(lambda s: s.lower(), sign_method.split('-'))
        assert mac_type == 'hmac'
        assert hash_type in hashlib.algorithms_guaranteed
    except (ValueError, AssertionError):
        return None

    body = await request.read()
    body_hash = hashlib.new(hash_type, body).hexdigest()
    sign_bytes = '{0}\n{1}\n{2}\nhost:{3}\ncontent-type:{4}\nx-sorna-version:{5}\n{6}'.format(
        request.method, str(request.rel_url), request.date.isoformat(),
        request.host, request.content_type, request.headers['X-Sorna-Version'],
        body_hash
    ).encode()

    sign_key = hmac.new(secret_key.encode(),
                        request.date.strftime('%Y%m%d').encode(),
                        hash_type).digest()
    sign_key = hmac.new(sign_key, request.host.encode(), hash_type).digest()
    return hmac.new(sign_key, sign_bytes, hash_type).hexdigest()


async def auth_middleware_factory(app, handler):
    '''
    Fetches user information and sets up keypair, uesr, and is_authorized attributes.
    '''
    async def auth_middleware_handler(request):
        request.is_authorized = False
        request.keypair = None
        request.user = None
        if not check_date(request):
            raise InvalidAuthParameters
        params = _extract_auth_params(request)
        if params:
            sign_method, access_key, signature = params
            async with app.dbpool.acquire() as conn, conn.transaction():
                j = sa.join(KeyPair, User,
                            KeyPair.c.belongs_to == User.c.id)
                query = sa.select([KeyPair, User]) \
                          .select_from(j) \
                          .where(KeyPair.c.access_key == access_key)
                row = await conn.fetchrow(query)
                if row is None:
                    raise AuthorizationFailed
                my_signature = await sign_request(sign_method, request, row.secret_key)
                if not my_signature:
                    raise InvalidAuthParameters
                if my_signature == signature:
                    query = KeyPair.update() \
                                   .values(last_used=datetime.utcnow(),
                                           total_num_queries=KeyPair.c.total_num_queries + 1,
                                           num_queries=KeyPair.c.num_queries + 1) \
                                   .where(KeyPair.c.access_key == access_key)
                    await conn.fetchval(query)
                    request.is_authorized = True
                    request.keypair = {
                        'access_key': access_key,
                        'secret_key': row.secret_key,
                        'concurrency_limit': row.concurrency_limit,
                        'rate_limit': row.rate_limit,
                        'remaining_cpu': row.remaining_cpu,
                        'remaining_mem': row.remaining_mem,
                        'remaining_io': row.remaining_io,
                        'remaining_net': row.remaining_net,
                    }
                    request.user = {
                        'id': row.id,
                        'email': row.email,
                    }
        # No matter if authenticated or not, pass-through to the handler.
        # (if it's required, auth_required decorator will handle the situation.)
        return (await handler(request))
    return auth_middleware_handler


def auth_required(handler):
    @functools.wraps(handler)
    async def wrapped(request):
        if request.is_authorized:
            return (await handler(request))
        raise AuthorizationFailed
    return wrapped


@auth_required
async def authorize(request) -> web.Response:
    try:
        params = json.loads(await request.text())
    except json.decoder.JSONDecodeError:
        raise InvalidAuthParameters
    resp_data = {'authorized': 'yes'}
    if 'echo' in params:
        resp_data['echo'] = params['echo']
    return web.json_response(resp_data)


async def init(app):
    app.router.add_route('GET', '/v1/authorize', authorize)
    app.middlewares.append(auth_middleware_factory)


async def shutdown(app):
    pass
