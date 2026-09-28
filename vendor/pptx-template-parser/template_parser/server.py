"""Local development API. Immutable exports; no decomposition editing routes."""
import hmac
import io
import json
import re
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from .batch import atomic_json, run_batch
from .extract import extract_template, sha256
from .render import render_package
from .exporter import export_package

MAX_UPLOAD = 50 * 1024 * 1024


def validate_upload(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if len(infos) > 20000 or sum(i.file_size for i in infos) > 500 * 1024 * 1024:
            raise ValueError('PPTX exceeds unpacked size limit')
        if not {'[Content_Types].xml', 'ppt/presentation.xml'} <= set(archive.namelist()):
            raise ValueError('Expected a PPTX presentation')
        if any('vbaproject' in i.filename.lower() for i in infos):
            raise ValueError('Macro-enabled presentations are not supported')


class Jobs:
    def __init__(self, root, config):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.capacity = threading.BoundedSemaphore(4)
        for state in self.root.glob('*/state.json'):
            record = json.loads(state.read_text(encoding='utf-8'))
            if record['status'] in ('queued', 'extracting', 'rendering', 'analyzing', 'exporting'):
                record['status'] = 'interrupted'
                atomic_json(state, record)

    def folder(self, job_id):
        if not re.fullmatch(r'[a-f0-9]{32}', job_id):
            raise FileNotFoundError()
        folder = self.root/job_id
        if not (folder/'state.json').is_file():
            raise FileNotFoundError()
        return folder

    def state(self, job_id):
        folder = self.folder(job_id)
        result = json.loads((folder/'state.json').read_text(encoding='utf-8'))
        progress = folder/'package/analysis_report.json'
        if progress.exists():
            try:
                report = json.loads(progress.read_text(encoding='utf-8'))
                result['progress'] = {'counts': report['counts'], 'total': len(report['slides'])}
            except (ValueError, KeyError):
                pass
        return result

    def update(self, job_id, **values):
        with self.lock:
            path = self.folder(job_id)/'state.json'
            record = json.loads(path.read_text(encoding='utf-8'))
            record.update(values)
            atomic_json(path, record)

    def submit(self, data):
        validate_upload(data)
        if not self.capacity.acquire(blocking=False):
            raise OverflowError('Queue is full')
        job_id = uuid.uuid4().hex
        try:
            folder = self.root/job_id
            folder.mkdir()
            (folder/'upload.pptx').write_bytes(data)
            atomic_json(folder/'state.json', {'job_id':job_id, 'status':'queued', 'version':None})
            self.executor.submit(self.process, job_id)
            return job_id
        except Exception:
            self.capacity.release()
            raise

    def resume(self, job_id):
        with self.lock:
            path = self.folder(job_id)/'state.json'
            record = json.loads(path.read_text(encoding='utf-8'))
            if record['status'] not in ('partial', 'failed', 'interrupted'):
                raise ValueError('Job cannot be resumed in this state')
            if not self.capacity.acquire(blocking=False):
                raise OverflowError('Queue is full')
            record['status'] = 'queued'
            atomic_json(path, record)
            self.executor.submit(self.process, job_id)

    def process(self, job_id):
        try:
            folder = self.folder(job_id)
            package = folder/'package'
            if not package.exists():
                self.update(job_id, status='extracting')
                extract_template(folder/'upload.pptx', package)
            if not (package/'previews').exists():
                self.update(job_id, status='rendering')
                render_package(package, renderer=self.config.get('renderer', 'auto'))
            self.update(job_id, status='analyzing')
            report = run_batch(package, self.config)
            self.update(job_id, status='exporting')
            version = uuid.uuid4().hex
            exported = export_package(package, folder/'versions'/version)
            self.update(job_id, status='completed' if report['status']=='finished' else 'partial',
                        version=version, exported=exported['exported'], counts=exported['counts'],
                        archive_sha256=sha256(Path(exported['archive']).read_bytes()), error=None)
        except Exception as exc:
            # Do not expose provider messages, credentials or local paths to clients.
            self.update(job_id, status='failed', error=type(exc).__name__)
        finally:
            self.capacity.release()

    def version(self, job_id, version):
        if not re.fullmatch(r'[a-f0-9]{32}', version):
            raise FileNotFoundError()
        folder = self.folder(job_id)/'versions'/version
        if not folder.with_suffix('.zip').exists():
            raise FileNotFoundError()
        return folder


def make_server(jobs, token, port=8765, origin=None):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def log_message(self, *args):
            pass

        def reply(self, status, value, mime='application/json'):
            data = json.dumps(value, ensure_ascii=False).encode('utf-8') if mime=='application/json' else value
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            if origin and self.headers.get('Origin') == origin:
                self.send_header('Access-Control-Allow-Origin', origin)
                self.send_header('Vary', 'Origin')
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            return (not self.headers.get('Origin') or self.headers.get('Origin') == origin) and hmac.compare_digest(
                self.headers.get('Authorization', ''), 'Bearer '+token)

        def do_OPTIONS(self):
            if not origin or self.headers.get('Origin') != origin:
                return self.reply(403, {'error':'Origin not allowed'})
            self.send_response(204)
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')
            self.send_header('Content-Length','0')
            self.end_headers()

        def dispatch(self, method):
            if not self.authorized():
                return self.reply(401, {'error':'Unauthorized'})
            parts = urlsplit(self.path).path.strip('/').split('/')
            try:
                if method=='POST' and parts==['jobs']:
                    if self.headers.get('Transfer-Encoding'):
                        return self.reply(400, {'error':'Content-Length required; chunked upload unsupported'})
                    size = int(self.headers.get('Content-Length','0'))
                    if not 0 < size <= MAX_UPLOAD:
                        return self.reply(413, {'error':'Upload must be 1..50 MiB'})
                    if self.headers.get('Content-Type','').split(';')[0] not in ('application/vnd.openxmlformats-officedocument.presentationml.presentation','application/octet-stream'):
                        return self.reply(415, {'error':'Send raw PPTX bytes, not multipart'})
                    data = self.rfile.read(size)
                    if len(data)!=size:
                        raise ValueError('Incomplete upload')
                    job_id=jobs.submit(data)
                    return self.reply(202, {'job_id':job_id, 'status_url':f'/jobs/{job_id}'})
                if len(parts)>=2 and parts[0]=='jobs':
                    job_id=parts[1]
                    if method=='GET' and len(parts)==2:
                        return self.reply(200, jobs.state(job_id))
                    if method=='POST' and parts[2:]==['resume']:
                        if int(self.headers.get('Content-Length','0')):
                            raise ValueError('Resume accepts no body')
                        jobs.resume(job_id)
                        return self.reply(202, {'job_id':job_id, 'status':'queued'})
                    if method=='GET' and len(parts)>=5 and parts[2]=='versions':
                        version=parts[3]
                        folder=jobs.version(job_id,version)
                        base=f'/jobs/{job_id}/versions/{version}'
                        if parts[4:]==['result']:
                            catalog=json.loads((folder/'catalog.json').read_text(encoding='utf-8'))
                            return self.reply(200, {'job_id':job_id,'version':version,'partial':catalog['partial'],
                                'slides':[{**{k:s[k] for k in ('slide_id','components','usage','needs_review','unassigned')},
                                           'preview_url':base+'/previews/'+s['slide_id']+'.png'} for s in catalog['slides']],
                                'validation':json.loads((folder/'validation_report.json').read_text(encoding='utf-8')),
                                'package_url':base+'/package'})
                        if parts[4:]==['package']:
                            return self.reply(200, folder.with_suffix('.zip').read_bytes(), 'application/zip')
                        if len(parts)==6 and parts[4]=='previews' and re.fullmatch(r's[1-9][0-9]*\.png',parts[5]):
                            return self.reply(200,(folder/'previews'/parts[5]).read_bytes(),'image/png')
                self.reply(404, {'error':'Route not found'})
            except FileNotFoundError:
                self.reply(404, {'error':'Not found'})
            except OverflowError:
                self.reply(429, {'error':'Local job queue full'})
            except (ValueError, zipfile.BadZipFile):
                self.reply(400, {'error':'Invalid request or job state'})
            except Exception:
                self.reply(500, {'error':'Internal error'})

        def do_GET(self): self.dispatch('GET')
        def do_POST(self): self.dispatch('POST')
        def do_PUT(self): self.reply(405, {'error':'Read-only decomposition'})
        do_PATCH = do_PUT
        do_DELETE = do_PUT

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def serve(root, config, token, port=8765, origin=None):
    if len(token)<24:
        raise ValueError('Local API token must contain at least 24 characters')
    jobs=Jobs(root,config)
    server=make_server(jobs,token,port,origin)
    print(f'Local parser API: http://127.0.0.1:{server.server_port}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        jobs.executor.shutdown(wait=True, cancel_futures=True)
