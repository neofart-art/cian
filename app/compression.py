"""JSON gzip with explicit q-value negotiation, without HTTP response caching."""
import gzip
import time
from starlette.datastructures import MutableHeaders


def qualities(value):
    result={}
    for part in value.lower().split(','):
        name,*params=part.strip().split(';')
        name=name.strip()
        if not name:continue
        q=1.
        for param in params:
            key,_,raw=param.strip().partition('=')
            if key=='q':
                try:q=float(raw)
                except ValueError:q=0.
        result[name]=q if 0<=q<=1 else 0.
    return result


class JsonGzipMiddleware:
    def __init__(self,app,minimum_size=700):
        self.app=app;self.minimum_size=minimum_size

    async def __call__(self,scope,receive,send):
        if scope['type']!='http':
            return await self.app(scope,receive,send)
        accept=','.join(v.decode('latin1') for k,v in scope.get('headers',[]) if k.lower()==b'accept-encoding')
        q=qualities(accept);gz=q.get('gzip',q.get('*',0))
        identity=q.get('identity',0 if q.get('*')==0 else 1)
        start=None;chunks=[];buffer=False;began=time.perf_counter()
        async def output(message):
            nonlocal start,buffer
            if message['type']=='http.response.start':
                start=message;headers=MutableHeaders(scope=start)
                buffer=('application/json' in headers.get('content-type','') and
                        'content-encoding' not in headers and not scope['path'].startswith('/api/auth/'))
                if not buffer:await send(message)
            elif message['type']=='http.response.body' and buffer:
                chunks.append(message.get('body',b''))
                if message.get('more_body'):return
                body=b''.join(chunks);headers=MutableHeaders(scope=start)
                varied={v.strip().lower() for v in headers.get('vary','').split(',')}
                if '*' not in varied and 'accept-encoding' not in varied:
                    headers.add_vary_header('Accept-Encoding')
                use_gzip=gz>0 and (len(body)>=self.minimum_size or identity==0)
                if 'identity' in q and q['identity']>gz:use_gzip=False
                if use_gzip:
                    stamp=time.perf_counter();body=gzip.compress(body,compresslevel=4,mtime=0)
                    headers['content-encoding']='gzip'
                    timing=headers.get('server-timing','')
                    headers['server-timing']=(timing+', ' if timing else '')+'gzip;dur='+str(round((time.perf_counter()-stamp)*1000,3))
                elif identity==0:
                    start['status']=406;body=b'{"detail":"No acceptable content encoding"}'
                headers['content-length']=str(len(body))
                timing=headers.get('server-timing','')
                headers['server-timing']=(timing+', ' if timing else '')+'app;dur='+str(round((time.perf_counter()-began)*1000,3))
                await send(start);await send({'type':'http.response.body','body':body})
            else:await send(message)
        await self.app(scope,receive,output)
