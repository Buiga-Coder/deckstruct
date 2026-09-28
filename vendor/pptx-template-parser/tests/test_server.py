import io
import json
import threading
import zipfile
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from unittest import TestCase
from unittest.mock import patch
from test_pipeline import test_directory
from template_parser.server import Jobs, make_server
from template_parser.batch import atomic_json


class ServerTests(TestCase):
    def test_partial_pipeline_publishes_version_without_editing_input(self):
        with test_directory() as root:
            jobs=Jobs(root,{'model':'mock'})
            job='b'*32
            folder=root/job
            folder.mkdir()
            (folder/'upload.pptx').write_bytes(b'input')
            atomic_json(folder/'state.json',{'job_id':job,'status':'queued','version':None})
            def extract(source,target): target.mkdir()
            def export(package,target):
                target.parent.mkdir()
                target.mkdir()
                target.with_suffix('.zip').write_bytes(b'output')
                return {'archive':str(target.with_suffix('.zip')),'exported':1,'counts':{'valid':1,'unavailable':1}}
            jobs.capacity.acquire()
            with patch('template_parser.server.extract_template',side_effect=extract), \
                 patch('template_parser.server.render_package'), \
                 patch('template_parser.server.run_batch',return_value={'status':'stopped_api_error'}), \
                 patch('template_parser.server.export_package',side_effect=export):
                jobs.process(job)
            state=jobs.state(job)
            self.assertEqual(state['status'],'partial')
            self.assertEqual(len(state['version']),32)
            self.assertTrue(jobs.version(job,state['version']).exists())
            self.assertEqual((folder/'upload.pptx').read_bytes(),b'input')
            jobs.executor.shutdown()

    def test_authenticated_upload_status_read_only_and_restart(self):
        with test_directory() as root:
            jobs=Jobs(root,{})
            server=make_server(jobs,'x'*24,0,'http://localhost:5173')
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            base=f'http://127.0.0.1:{server.server_port}'
            def request(path,method='GET',data=None,auth=True,**headers):
                if auth: headers['Authorization']='Bearer '+'x'*24
                return urlopen(Request(base+path,data=data,headers=headers,method=method),timeout=5)
            try:
                with self.assertRaises(HTTPError) as caught: request('/jobs/no',auth=False)
                self.assertEqual(caught.exception.code,401)
                data=io.BytesIO()
                with zipfile.ZipFile(data,'w') as z:
                    z.writestr('[Content_Types].xml','test')
                    z.writestr('ppt/presentation.xml','test')
                with patch.object(jobs.executor,'submit') as submit:
                    with request('/jobs','POST',data.getvalue(),**{'Content-Type':'application/octet-stream'}) as response:
                        self.assertEqual(response.status,202)
                        job=json.load(response)['job_id']
                    self.assertEqual(submit.call_count,1)
                with request('/jobs/'+job) as response:
                    self.assertEqual(json.load(response)['status'],'queued')
                with self.assertRaises(HTTPError) as caught: request('/jobs/'+job,'PATCH',b'{}')
                self.assertEqual(caught.exception.code,405)
                with self.assertRaises(HTTPError) as caught: request('/jobs/'+job,Origin='http://evil.invalid')
                self.assertEqual(caught.exception.code,401)
                # Immutable version used by both frontend and generator.
                version='a'*32
                folder=jobs.folder(job)/'versions'/version
                folder.mkdir(parents=True)
                folder.with_suffix('.zip').write_bytes(b'zip fixture')
                atomic_json(folder/'catalog.json',{'partial':True,'slides':[]})
                atomic_json(folder/'validation_report.json',{'counts':{'unavailable':1}})
                with request(f'/jobs/{job}/versions/{version}/result') as response:
                    result=json.load(response)
                    self.assertEqual(result['version'],version)
                    self.assertIn('package_url',result)
                restarted=Jobs(root,{})
                self.assertEqual(restarted.state(job)['status'],'interrupted')
                restarted.executor.shutdown()
            finally:
                server.shutdown(); server.server_close(); thread.join()
                jobs.executor.shutdown()
