"""One local cover per listing, plus a thumbnail, inside the existing /data volume.

No Cian credentials, cookies, page visits or gallery downloads. Only a rendered
card's HTTPS image on the public Cian image CDN is accepted. Requests are pinned
to a checked public IP, retaining hostname TLS verification; redirects are denied.
"""
import hashlib
import http.client
import io
import ipaddress
import logging
import os
from pathlib import Path
import queue
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit, urlunsplit
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

log = logging.getLogger('xata')
IMAGE_HOSTS = frozenset({'images.cdn-cian.ru'})
MAX_BYTES = 6 * 1024 * 1024
MAX_PIXELS = 16_000_000
CONNECT_TIMEOUT = 2
READ_TIMEOUT = 2
TOTAL_SECONDS = 6
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
RELATIVE_PATH = re.compile(r'images/listings/([0-9]{1,32})-([a-f0-9]{24})-(normal|thumb)\.jpg')


def cover_url(value):
    """Pure validation: safe to use during a rolled-back CSV preview."""
    if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(c) <= 32 or ord(c) > 126 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or parsed.hostname not in IMAGE_HOSTS or parsed.username or parsed.password
                or parsed.port not in (None,443) or parsed.fragment or not parsed.path.startswith('/')):
            return None
        return urlunsplit(('https',parsed.hostname,parsed.path,parsed.query,''))
    except ValueError:
        return None


def public_address(host):
    records = socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(record[4][0] for record in records))
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError('Non-public image address')
    return addresses[0]


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host,443,timeout=CONNECT_TIMEOUT,context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address,443),self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw,server_hostname=self.host)
            self.sock.settimeout(READ_TIMEOUT)
        except BaseException:
            raw.close()
            raise


class ImageDownloadError(Exception):
    def __init__(self, status):
        self.status = status


def download_cover(url):
    url = cover_url(url)
    if url is None:
        raise ValueError('Unsupported image URL')
    parsed = urlsplit(url)
    # Connect to this IP directly, never resolving the hostname again inside
    # the HTTPS client. TLS SNI and Host remain the original allowlisted host.
    conn = PinnedHTTPSConnection(parsed.hostname,public_address(parsed.hostname))
    started = time.monotonic()
    try:
        path = parsed.path + ('?' + parsed.query if parsed.query else '')
        conn.request('GET',path,headers={'Accept':'image/jpeg,image/png,image/webp,image/avif',
                                      'User-Agent':'XataCover/1.0','Accept-Encoding':'identity'})
        response = conn.getresponse()
        if response.status != 200:  # Includes redirects; never follow their Location.
            raise ImageDownloadError(response.status)
        if response.getheader('Content-Encoding','identity').lower() != 'identity':
            raise ValueError('Encoded image response')
        length = response.getheader('Content-Length')
        if length is not None and (not length.isdecimal() or int(length) > MAX_BYTES):
            raise ValueError('Image exceeds byte limit')
        if response.getheader('Content-Type','').split(';')[0].lower() not in {
                'image/jpeg','image/png','image/webp','image/avif','application/octet-stream'}:
            raise ValueError('Unsupported image MIME type')
        data = bytearray()
        while True:
            remaining = TOTAL_SECONDS - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError('Image deadline')
            if conn.sock is not None:
                conn.sock.settimeout(min(READ_TIMEOUT,remaining))
            chunk = response.read1(min(64*1024,MAX_BYTES-len(data)+1))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_BYTES:
                raise ValueError('Image exceeds byte limit')
        if not data:
            raise ValueError('Empty image')
        return bytes(data)
    finally:
        conn.close()


def resize_cover(data):
    if len(data) > MAX_BYTES:
        raise ValueError('Image exceeds byte limit')
    with warnings.catch_warnings():
        warnings.simplefilter('error',Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as source:
            if (source.format not in {'JPEG','PNG','WEBP','AVIF'} or source.width*source.height > MAX_PIXELS
                    or getattr(source,'n_frames',1) != 1):
                raise ValueError('Unsupported image dimensions or format')
            # JPEG draft decoding and resizing before RGB conversion keep the
            # single worker within the existing app container's memory budget.
            source.draft('RGB',(1600,1200))
            source.load()
            oriented = ImageOps.exif_transpose(source)
            oriented.thumbnail((1600,1200),Image.Resampling.LANCZOS)
            rgb = Image.new('RGB',oriented.size,(244,243,236))
            if 'A' in oriented.getbands():
                rgb.paste(oriented,mask=oriented.getchannel('A'))
            else:
                rgb.paste(oriented.convert('RGB'))
            oriented.close()
            normal = io.BytesIO()
            rgb.save(normal,format='JPEG',quality=82,optimize=True)
            thumb = ImageOps.fit(rgb,(220,160),method=Image.Resampling.LANCZOS)
            thumbnail = io.BytesIO()
            thumb.save(thumbnail,format='JPEG',quality=78,optimize=True)
            thumb.close()
            rgb.close()
            return normal.getvalue(),thumbnail.getvalue()


class CoverCache:
    def __init__(self, data_dir, database):
        self.data = Path(data_dir)
        self.root = self.data/'images/listings'
        self.database = database
        self.jobs = queue.Queue(maxsize=500)
        self.pending = set()
        self.again = set()
        self.guard = threading.Lock()
        self.stop = threading.Event()
        self.thread = None

    def path(self, relative):
        if not isinstance(relative,str) or not RELATIVE_PATH.fullmatch(relative):
            return None
        path = self.data/relative
        if path.resolve().parent != self.root.resolve():
            return None
        return path

    def sources(self, row):
        normal,thumb = row['cover_image_path'],row['cover_thumb_path']
        if not normal or not thumb:
            return {'cover_image_src':None,'cover_thumb_src':None}
        match = RELATIVE_PATH.fullmatch(normal)
        thumb_match = RELATIVE_PATH.fullmatch(thumb)
        if (not match or not thumb_match or match[1] != str(row['id']) or thumb_match[1] != match[1]
                or thumb_match[2] != match[2] or match[3]!='normal' or thumb_match[3]!='thumb'
                or self.path(normal) is None or self.path(thumb) is None):
            return {'cover_image_src':None,'cover_thumb_src':None}
        prefix = '/api/listings/'+match[1]+'/cover/'+match[2]+'/'
        return {'cover_image_src':prefix+'normal','cover_thumb_src':prefix+'thumb'}

    def enqueue(self, listing_ids):
        with self.guard:
            if self.stop.is_set():
                return
            for listing_id in listing_ids:
                listing_id = str(listing_id)
                if listing_id in self.pending:
                    self.again.add(listing_id)
                    continue
                try:
                    self.jobs.put_nowait(listing_id)
                    self.pending.add(listing_id)
                except queue.Full:
                    log.warning('cover queue full; retry on next update')
                    break
            if self.pending and self.thread is None:
                self.thread = threading.Thread(target=self._run,name='xata-cover-cache',daemon=True)
                self.thread.start()

    def _run(self):
        while not self.stop.is_set():
            try:
                listing_id = self.jobs.get(timeout=.2)
            except queue.Empty:
                continue
            try:
                self.sync_one(listing_id)
            finally:
                with self.guard:
                    self.pending.discard(listing_id)
                    if listing_id in self.again:
                        self.again.discard(listing_id)
                        try:
                            self.jobs.put_nowait(listing_id)
                            self.pending.add(listing_id)
                        except queue.Full:
                            pass
                self.jobs.task_done()

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(2)

    def _write(self, relative, data):
        path = self.path(relative)
        if path is None:
            raise ValueError('Invalid cache path')
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        temporary = path.with_name(path.name+'.tmp')
        try:
            with temporary.open('wb') as stream:
                stream.write(data)
            temporary.chmod(0o600)
            os.replace(temporary,path)
        finally:
            temporary.unlink(missing_ok=True)

    def _remove(self, relative):
        path = self.path(relative)
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                log.warning('cover old-file cleanup skipped')

    def _preferred(self, conn, property_id):
        return conn.execute('''SELECT l.* FROM listings l JOIN properties p ON p.id=l.property_id
            WHERE l.property_id=? AND l.cover_image_url IS NOT NULL
            ORDER BY (l.id=p.current_listing_id) DESC,l.first_seen DESC,l.rowid DESC LIMIT 1''',
                            (property_id,)).fetchone()

    def _commit(self, listing_id, url, paths):
        removed = []
        with self.database() as conn:
            conn.execute('BEGIN IMMEDIATE')
            current = conn.execute('SELECT * FROM listings WHERE id=?',(listing_id,)).fetchone()
            preferred = self._preferred(conn,current['property_id']) if current else None
            if preferred is None or preferred['id'] != listing_id or preferred['cover_image_url'] != url:
                return False
            for other in conn.execute('SELECT * FROM listings WHERE property_id=?',(current['property_id'],)).fetchall():
                removed.extend(path for path in (other['cover_image_path'],other['cover_thumb_path']) if path and path not in paths)
            conn.execute('''UPDATE listings SET cover_image_path=NULL,cover_thumb_path=NULL,cover_image_cached_url=NULL
                            WHERE property_id=? AND id<>?''',(current['property_id'],listing_id))
            conn.execute('''UPDATE listings SET cover_image_path=?,cover_thumb_path=?,cover_image_cached_url=?
                            WHERE id=?''',(*paths,url,listing_id))
        for relative in set(removed):
            self._remove(relative)
        return True

    def sync_one(self, listing_id):
        """A failed image never rolls back the already committed listing."""
        try:
            if not re.fullmatch(r'[0-9]{1,32}',str(listing_id)):
                return
            with self.database() as conn:
                row = conn.execute('SELECT * FROM listings WHERE id=?',(listing_id,)).fetchone()
                if row is not None:
                    preferred = self._preferred(conn,row['property_id'])
                    if preferred is None or preferred['id'] != listing_id:
                        return
            if row is None or cover_url(row['cover_image_url']) is None:
                return
            url = row['cover_image_url']
            old = (row['cover_image_path'],row['cover_thumb_path'])
            if row['cover_image_cached_url']==url and all(self.path(p) is not None and self.path(p).is_file() for p in old):
                self._commit(listing_id,url,old)
                return
            # Relistings may retain the same URL: reuse the local pair.
            with self.database() as conn:
                reusable = conn.execute('''SELECT * FROM listings WHERE property_id=? AND cover_image_cached_url=?
                    AND cover_image_path IS NOT NULL AND cover_thumb_path IS NOT NULL''',
                                        (row['property_id'],url)).fetchall()
            normal = thumb = None
            for previous in reusable:
                pair = (self.path(previous['cover_image_path']),self.path(previous['cover_thumb_path']))
                if all(path is not None and path.is_file() for path in pair):
                    normal,thumb = (path.read_bytes() for path in pair)
                    break
            if normal is None:
                normal,thumb = resize_cover(download_cover(url))
            version = hashlib.sha256(normal).hexdigest()[:24]
            paths = tuple(f'images/listings/{listing_id}-{version}-{size}.jpg' for size in ('normal','thumb'))
            for relative,data in zip(paths,(normal,thumb)):
                self._write(relative,data)
            if not self._commit(listing_id,url,paths):
                with self.database() as conn:
                    current = conn.execute('SELECT cover_image_path,cover_thumb_path FROM listings WHERE id=?',(listing_id,)).fetchone()
                for path in paths:
                    if not current or path not in tuple(current):
                        self._remove(path)
        except Exception as exc:
            # Never log URL/query strings, headers, image bytes or credentials.
            log.warning('cover update skipped listing_id=%s error_type=%s http_status=%s',
                        listing_id,type(exc).__name__,exc.status if isinstance(exc,ImageDownloadError) else '-')
