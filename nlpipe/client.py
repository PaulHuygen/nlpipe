import hashlib
import time
import os.path
import errno
import logging

import requests

from nlpipe.module import Module, get_module

# Status definitions and subdir names
STATUS = {"PENDING": "queue",
          "STARTED": "inprogress",
          "DONE": "results",
          "ERROR": "errors"}


def get_id(doc):
    """
    Calculate the id (hash) of the given document
    :param doc: The document (string)
    :return: a task id (hash)
    """
    if len(doc) == 34 and doc.startswith("0x"):
        # it sure looks like a hash
        return doc
    m = hashlib.md5()
    if isinstance(doc, str):
        doc = doc.encode("utf-8")
    m.update(doc)
    return "0x" + m.hexdigest()

class Client(object):
    """Abstract class for NLPipe client bindings"""

    def process(self, module, doc):
        """Add a document to be processed by module, returning the task ID
        :param module: Module name
        :param doc: A document (string)
        :return: task ID
        :rtype: str
        """
        raise NotImplementedError()

    def status(self, module, id):
        """Get processing status
        :param module: Module name
        :param id: Task ID
        :return: any of 'UNKNOWN', 'PENDING', 'STARTED', 'DONE', 'ERROR'
        """
        raise NotImplementedError()

    def result(self, module, id, format=None):
        """Get processing result, optionally converted to a specified format
        :param module: Module name
        :param id: A document (string) or task ID
        :param format: (Optional) format to convert to, e.g. 'xml', 'csv', 'json'
        :return: The result of processing (string)
        """
        raise NotImplementedError()

    def process_inline(self, module, doc):
        """
        Process the given document, use cached version if possible, wait and return result
        :param module: Module name
        :param doc: A document (string)
        :return: The result of processing (string)
        """
        id = get_id(doc)
        if self.status(module, id) == 'UNKNOWN':
            self.process(module, doc)
        while True:
            status = self.status(module, id)
            if status in ('DONE', 'ERROR'):
                return self.result(module, id)
            time.sleep(0.1)

    def get_task(self, module):
        """
        Get a document to process with the given module, marking the document as 'in progress'
        :param module: Name of the module
        :return: a pair (id, string) for the document to be processed
        """
        raise NotImplementedError()

    def get_tasks(self, module, n):
        """
        Get multiple documents to process
        :param module: Name of the module for processing
        :param n: Number of documents to retrieve
        :return: a sequence of (id, document string) pairs
        """
        for i in range(n):
            yield self.get_task(module)

    def store_result(self, module, id, result):
        """
        Store the given result
        :param module: Module name
        :param id: Document or task ID
        :param result: Result (string)
        """
        raise NotImplementedError()

    def store_error(self, module, id, result):
        """
        Store an error
        :param module: Module name
        :param id: Document or task ID
        :param result: Result (string) describing the error
        """
        raise NotImplementedError()

class FSClient(Client):
    """
    NLPipe client that relies on direct filesystem access (e.g. on local machine or over NFS)
    """

    def __init__(self, result_dir):
        self.result_dir = result_dir

    def _check_dirs(self, module):
        for subdir in STATUS.values():
            dirname = os.path.join(self.result_dir, module, subdir)
            try:
                os.makedirs(dirname)
            except OSError as e:
                if e.errno != errno.EEXIST:
                    raise

    def _write(self, module, status, id, doc):
        self._check_dirs(module)
        fn = self._filename(module, status, id)
        open(fn, 'w', encoding="UTF-8").write(doc)
        return fn

    def _read(self, module, status, id):
        fn = self._filename(module, status, id)
        return open(fn, encoding="UTF-8").read()

    def _move(self, module, id, from_status, to_status):
        fn_from = self._filename(module, from_status, id)
        fn_to = self._filename(module, to_status, id)
        os.rename(fn_from, fn_to)

    def _delete(self, module, status, id):
        fn = self._filename(module, status, id)
        os.remove(fn)

    def _filename(self, module, status, id=None):
        dirname = os.path.join(self.result_dir, module, STATUS[status])
        if id is None:
            return dirname
        else:
            return os.path.join(dirname, id)

    def status(self, module, id):
        for status in STATUS.keys():
            if os.path.exists(self._filename(module, status, id)):
                return status
        return 'UNKNOWN'

    def process(self, module, doc):
        id = get_id(doc)
        if self.status(module, id) == 'UNKNOWN':
            self._write(module, 'PENDING', id, doc)
        return id

    def result(self, module, id, format=None):
        status = self.status(module, id)
        if status == 'DONE':
            result = self._read(module, 'DONE', id)
            if format is not None:
                result = get_module(module).convert(result, format)
            return result
        if status == 'ERROR':
            raise Exception(self._read(module, 'ERROR', id))
        raise ValueError("Status of {id} is {status}".format(**locals()))

    def get_task(self, module):
        path = self._filename(module, 'PENDING')
        try:
            files = os.listdir(path)
        except OSError as e:
            if e.errno == errno.ENOENT:
                return None, None
            raise
        if not files:
            return None, None
        id = min(files, key=lambda f: os.path.getctime(os.path.join(path, f)))
        self._move(module, id, 'PENDING', 'STARTED')
        return id, self._read(module, 'STARTED', id)

    def store_result(self, module, id, result):
        status = self.status(module, id)
        if status not in ('STARTED', 'DONE', 'ERROR'):
            raise ValueError("Cannot store result for task {id} with status {status}".format(**locals()))
        self._write(module, 'DONE', id, result)
        if status in ('STARTED', 'ERROR'):
            self._delete(module, status, id)

    def store_error(self, module, id, result):
        status = self.status(module, id)
        if status not in ('STARTED', 'DONE', 'ERROR'):
            raise ValueError("Cannot store result for task {id} with status {status}".format(**locals()))
        self._write(module, 'ERROR', id, result)
        if status in ('STARTED', 'DONE'):
            self._delete(module, status, id)

class HTTPClient(Client):
    """
    NLPipe client that connects to the REST server
    """

    def __init__(self, server="http://localhost:5000"):
        self.server = server

    def status(self, module, id):
        url = "{self.server}/api/modules/{module}/{id}".format(**locals())
        res = requests.head(url)
        if 'Status' in res.headers:
            return(res.headers['Status'])
        raise Exception("Cannot determine status for {module}/{id}; return code: {res.status_code}"
                        .format(**locals()))
        
    def process(self, module, doc):
        url = "{self.server}/api/modules/{module}/".format(**locals())
        res = requests.post(url, data=doc)
        if res.status_code != 202:
            raise Exception("Error on processing doc with {module}; return code: {res.status_code}:\n{res.text}"
                            .format(**locals()))
        return res.headers['ID']

    def result(self, module, id, format=None):
        url = "{self.server}/api/modules/{module}/{id}".format(**locals())
        if format is not None:
            url = "{url}?format={format}".format(**locals())
        res = requests.get(url)
        if res.status_code != 200:
            raise Exception("Error on getting result for {module}/{id}; return code: {res.status_code}:\n{res.text}"
                            .format(**locals()))
        return res.text

    def get_task(self, module):
        url = "{self.server}/api/modules/{module}/".format(**locals())
        res = requests.get(url)

        if res.status_code == 404:
            return None, None
        elif res.status_code != 200:
            raise Exception("Error on getting a task for {module}; return code: {res.status_code}:\n{res.text}"
                            .format(**locals()))
        return res.headers['ID'], res.text


    def store_result(self, module, id, result):
        url = "{self.server}/api/modules/{module}/{id}".format(**locals())
        res = requests.put(url, data=result)

        if res.status_code != 204:
            raise Exception("Error on getting a task for {module}; return code: {res.status_code}:\n{res.text}"
                            .format(**locals()))

def _get_client(servername):
    if servername.startswith("http:") or servername.startswith("https:"):
        logging.getLogger('requests').setLevel(logging.WARNING)
        logging.debug("Connecting to REST server at {servername}".format(**locals()))
        return HTTPClient(servername)
    else:
        logging.debug("Connecting to local repository {servername}".format(**locals()))
        return FSClient(servername)
        
if __name__ == '__main__':
    import argparse
    import sys
    
    parser = argparse.ArgumentParser()
    parser.add_argument("server", help="Server hostname or directory location")
    parser.add_argument("module", help="Module name")
    parser.add_argument("--verbose", "-v", help="Verbose (debug) output", action="store_true", default=False)
    action_parser = parser.add_subparsers(dest='action', title='Actions',)


    
    for action in 'status', 'result':
        action_parser.add_parser(action).add_argument('id', help="Task ID")
    for action in 'process', 'process_inline':
        p = action_parser.add_parser(action)
        p.add_argument('doc', help="Document to process (use - to read from stdin")
    action_parser.add_parser('get_task')
    for action in ('store_result', 'store_error'):
        p = action_parser.add_parser(action)
        p.add_argument('id', help="Task ID")
        p.add_argument('result', help="Document to store (use - to read from stdin")
    
    args = vars(parser.parse_args())  # turn to dict so we can pop and pass the rest as kargs

    logging.basicConfig(level=logging.DEBUG if args.pop('verbose', False) else logging.INFO,
                        format='[%(asctime)s %(name)-12s %(levelname)-5s] %(message)s')
    
    client = _get_client(args.pop('server'))
    
    for doc_arg in ('doc', 'result'):
        if args.get(doc_arg) == '-':
            args[doc_arg] = sys.stdin.read()

    action = args.pop('action')
    result = getattr(client, action)(**args)
    if action == "get_task":
        id, doc = result
        if id is not None:
            print(id, file=sys.stderr)
            print(doc)
    elif action in ("store_result", "store_error"):
        pass
    else:
        print(result)
