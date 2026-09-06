"""ASGI body limits, security headers and redacted errors."""
import asyncio
import json
import logging
import secrets
import time
from starlette.responses import JSONResponse
from starlette.concurrency import run_in_threadpool


class SecurityMiddleware:
    def __init__(self, app, settings, store):
        self.app, self.settings, self.store = app, settings, store

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        request_id = secrets.token_hex(16)
        started, status = False, 500
        headers = dict(scope['headers'])
        async def secured(message):
            nonlocal started, status
            if message['type'] == 'http.response.start':
                started, status = True, message['status']
                message['headers'] += [
                    (b'x-content-type-options', b'nosniff'), (b'x-frame-options', b'DENY'),
                    (b'referrer-policy', b'no-referrer'), (b'cache-control', b'no-store'),
                    (b'permissions-policy', b'camera=(), microphone=(), geolocation=()'),
                    (b'x-request-id', request_id.encode()),
                    (b'content-security-policy', b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")]
                if self.settings.production:
                    message['headers'].append((b'strict-transport-security', b'max-age=31536000'))
            await send(message)
        try:
            # ALB probes use a private-IP Host header. Expose only this fixed, non-sensitive route.
            if scope['path'] == '/api/health' and scope['method'] == 'GET':
                def ready():
                    from sqlalchemy import select
                    from store import states
                    with self.store.engine.connect() as conn:
                        conn.execute(select(states.c.id).limit(1))
                await run_in_threadpool(ready)
                return await JSONResponse({'status': 'ok'})(scope, receive, secured)
            if scope['method'] in ('POST', 'PUT', 'PATCH'):
                if headers.get(b'content-type', b'').split(b';')[0].strip() != b'application/json':
                    return await JSONResponse({'detail': 'JSON 요청이 필요합니다.'}, 415)(scope, receive, secured)
                chunks, size, deadline = [], 0, time.monotonic() + 10
                while True:
                    message = await asyncio.wait_for(receive(), timeout=max(0.001, deadline - time.monotonic()))
                    if message['type'] == 'http.disconnect':
                        return
                    chunk = message.get('body', b'')
                    size += len(chunk)
                    if size > self.settings.max_body:
                        return await JSONResponse({'detail': '요청이 너무 큽니다.'}, 413)(scope, receive, secured)
                    chunks.append(chunk)
                    if not message.get('more_body'):
                        break
                body, delivered = b''.join(chunks), False
                original_receive = receive
                async def buffered():
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {'type': 'http.request', 'body': body, 'more_body': False}
                    return await original_receive()
                receive = buffered
            if scope['path'].startswith('/api/') and scope['path'] != '/api/health':
                if not await run_in_threadpool(self.store.rate, 'global-api', 1200):
                    return await JSONResponse({'detail': '요청 한도 초과'}, 429)(scope, receive, secured)
            await self.app(scope, receive, secured)
        except Exception:
            logging.getLogger('security').error(json.dumps({'event': 'request_failed', 'request_id': request_id}))
            if not started:
                await JSONResponse({'detail': '처리할 수 없습니다. 잠시 후 다시 시도해 주세요.',
                                    'request_id': request_id}, 503)(scope, receive, secured)
        finally:
            logging.getLogger('security').info(json.dumps({'event': 'http', 'request_id': request_id,
                'status': status, 'owner': scope.get('state', {}).get('owner'), 'time': time.time()}))
