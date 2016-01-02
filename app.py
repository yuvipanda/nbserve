import tornado
import mimetypes
from tornado.httpclient import AsyncHTTPClient, HTTPRequest
import json
import os
from traitlets.config import Application, Configurable
from traitlets import Unicode, Integer, Type, Bool, List

from nbconvert.exporters import HTMLExporter


class PublishProvider(Configurable):
    @tornado.gen.coroutine
    def content_for_url_segment(self, url_segment):
        """
        Return a tuple of (file_like_obj, mimetype) to be served for this url segment
        """
        raise NotImplementedError('Override in subclass')


class NaiveFilesystemPublisher(PublishProvider):
    base_path = Unicode(
        os.getcwd(),
        config=True,
        help='The base path where user homedirs are stored',
    )

    def guess_mimetype(self, path):
        # Stolen from StaticFileHandler
        # shortcircuit .ipynb files
        # FIXME: Integrate this shortcircuit into the mimetypes module
        if path.endswith('.ipynb'):
            return 'application/x-ipynb+json'
        mime_type, encoding = mimetypes.guess_type(path)
        if encoding == "gzip":
            # per RFC 6713, use the appropriate type for a gzip compressed file
            return "application/gzip"
        elif encoding is not None:
            # As of 2015-07-21 there is no bzip2 encoding defined at
            # http://www.iana.org/assignments/media-types/media-types.xhtml
            # So for that (and any other encoding), use octet-stream.
            return "application/octet-stream"
        elif mime_type is not None:
            return mime_type
        else:
            # if mime_type not detected, use application/octet-stream
            return "application/octet-stream"

    @tornado.gen.coroutine
    def path_for_url_segment(self, url_segment):
        return os.path.join(self.base_path, url_segment)

    @tornado.gen.coroutine
    def content_for_url_segment(self, url_segment):
        path = yield self.path_for_url_segment(url_segment)
        mimetype = self.guess_mimetype(path)
        return (open(path), mimetype)


class MainHandler(tornado.web.RequestHandler):

    def __init__(self, *args, **kwargs):
        self.publisher = kwargs.pop('publisher')
        super().__init__(*args, **kwargs)

    @tornado.gen.coroutine
    def get(self, filename):
        exporter = HTMLExporter()
        file_handle, mimetype = yield self.publisher.content_for_url_segment(filename)
        try:
            NbServer.instance().log.error(mimetype)
            if mimetype == 'application/x-ipynb+json':
                html, res = exporter.from_file(file_handle)
                self.write(html)
            else:
                return self.handle_static_file(file_handle, mimetype)
        except FileNotFoundError:
            raise tornado.web.HTTPError(404)
        finally:
            # FIXME: Test and verify that this is actually closed properly@
            file_handle.close()

    @classmethod
    def get_chunked_content(cls, file, start=None, end=None):
        # Stolen and adapted from StaticFileHandler
        if start is not None:
            file.seek(start)
        if end is not None:
            remaining = end - (start or 0)
        else:
            remaining = None
        while True:
            chunk_size = 64 * 1024
            if remaining is not None and remaining < chunk_size:
                chunk_size = remaining
            chunk = file.read(chunk_size)
            if chunk:
                if remaining is not None:
                    remaining -= len(chunk)
                yield chunk
            else:
                if remaining is not None:
                    assert remaining == 0
                return

    @tornado.gen.coroutine
    def handle_static_file(self, file_handle, mimetype):
        # Stolen from StaticFileHandler
        self.set_header('Content-Type',  mimetype)
        content = MainHandler.get_chunked_content(file_handle)
        if isinstance(content, bytes):
            content = [content]
        for chunk in content:
            try:
                self.write(chunk)
                yield self.flush()
            except tornado.iostream.StreamClosedError:
                return


def register_proxy(proxy_api_url, path_prefix, target, auth_token):
    client = AsyncHTTPClient()
    url = proxy_api_url + path_prefix
    body = {'target': target}
    req = HTTPRequest(
        url,
        method='POST',
        headers={'Authorization': 'token {}'.format(auth_token)},
        body=json.dumps(body),
    )

    return client.fetch(req)


class NbServer(Application):
    base_url = Unicode(
        "/",
        config=True,
        help="Base URL prefix for nbserve. MUST have a trailing slash"
    )

    bind_ip = Unicode(
        '127.0.0.1',
        config=True,
        help='IP to bind to for the HTTP Server'
    )

    bind_port = Integer(
        8889,
        config=True,
        help='Port to bind to for the HTTP Server'
    )

    config_file = Unicode(
        'nbserve_config.py',
        config=True,
        help='Config file to load'
    )

    publisher_class = Type(
        NaiveFilesystemPublisher,
        PublishProvider,
        config=True,
        help='Class that provides publisher (loldocumentationgetbetter)'
    )

    classes = List([
        PublishProvider,
    ])

    proxy_api_url = Unicode(
        'http://127.0.0.1:8001/api/routes',
        config=True,
        help='Full API URL for the REST Configurable HTTP Proxy'
    )

    proxy_target = Unicode(
        config=True,
        help='The target (<proto>://<hostname>:<port>) that the proxy should route to'
    )

    def _proxy_target_default(self):
        return 'http://{}:{}'.format(self.bind_ip, self.bind_port)

    proxy_auth_token = Unicode(
        os.environ.get('CONFIGPROXY_AUTH_TOKEN', ''),
        config=True,
        help='Auth token to use when talking to the proxy'
    )
    register_proxy = Bool(
        True,
        config=True,
        help='Register nbserver with the Configurable HTTP Proxy (or not!)'
    )

    @tornado.gen.coroutine
    def initialize(self, *args, **kwargs):
        super().initialize(*args, **kwargs)
        self.load_config_file(self.config_file)

    def start(self):
        publisher = self.publisher_class(parent=self)
        app = tornado.web.Application([
            (r"{}(.*)".format(self.base_url), MainHandler, {'publisher': publisher}),
        ], autoreload=True)
        app.listen(self.bind_port, address=self.bind_ip)

        if self.register_proxy:
            tornado.ioloop.IOLoop.current().run_sync(lambda: register_proxy(
                self.proxy_api_url,
                self.base_url,
                self.proxy_target,
                self.proxy_auth_token,
            ))

        tornado.ioloop.IOLoop.current().start()


if __name__ == "__main__":
    NbServer.instance().initialize()
    NbServer.instance().start()
