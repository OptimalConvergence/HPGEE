"""Optional libcurl transport: same deadlines and validation, reusable connections."""
import json
from collections import defaultdict
import pycurl
import certifi
from .bounded import BoundedTransport


class CurlTransport(BoundedTransport):
    def __init__(self, *, reuse_connections=False, **kwargs):
        super().__init__(**kwargs)
        if not pycurl.version_info()[4] & pycurl.VERSION_ASYNCHDNS:
            raise RuntimeError("Asynchronous DNS is required for bounded libcurl requests")
        self.reuse_connections = reuse_connections
        self.idle = defaultdict(list)

    def _transfer(self, root, config, deadline):
        options = {};headers = []
        for line in config:
            key, value = (part.strip() for part in line.split('=',1))
            value = json.loads(value) if value.startswith('"') else value
            if key == 'header':headers.append(value)
            else:options[key]=value
        key = (options.get('proxy'),options.get('proxy-user'))
        with self.lock:
            curl = self.idle[key].pop() if self.reuse_connections and self.idle[key] else None
        if curl is None:curl = pycurl.Curl()
        curl.reset()
        curl.setopt(pycurl.URL,options['url'])
        curl.setopt(pycurl.CUSTOMREQUEST,options['request'])
        curl.setopt(pycurl.HTTPHEADER,headers)
        curl.setopt(pycurl.NOSIGNAL,1)
        curl.setopt(pycurl.CONNECTTIMEOUT_MS,max(1,int(min(10,deadline)*1000)))
        curl.setopt(pycurl.TIMEOUT_MS,max(1,int(deadline*1000)))
        curl.setopt(pycurl.SSL_VERIFYPEER,1);curl.setopt(pycurl.SSL_VERIFYHOST,2)
        curl.setopt(pycurl.CAINFO,certifi.where())
        curl.setopt(pycurl.ACCEPT_ENCODING,'')
        curl.setopt(pycurl.MAXCONNECTS,2)
        if 'proxy' in options:curl.setopt(pycurl.PROXY,options['proxy'])
        if 'noproxy' in options:curl.setopt(pycurl.NOPROXY,options['noproxy'])
        if 'proxy-user' in options:curl.setopt(pycurl.PROXYUSERPWD,options['proxy-user'])
        if 'data-binary' in options:
            from pathlib import Path
            curl.setopt(pycurl.POSTFIELDS,Path(options['data-binary'][1:]).read_bytes())
        code=0
        try:
            with (root/'body').open('wb') as body,(root/'headers').open('wb') as head:
                curl.setopt(pycurl.WRITEDATA,body);curl.setopt(pycurl.HEADERFUNCTION,head.write)
                try:curl.perform()
                except pycurl.error as error:code=error.args[0]
                metrics=dict(http_code=curl.getinfo(pycurl.RESPONSE_CODE),
                    time_starttransfer=curl.getinfo(pycurl.STARTTRANSFER_TIME),
                    time_total=curl.getinfo(pycurl.TOTAL_TIME),
                    num_connects=curl.getinfo(pycurl.NUM_CONNECTS),
                    time_connect=curl.getinfo(pycurl.CONNECT_TIME),
                    time_appconnect=curl.getinfo(pycurl.APPCONNECT_TIME),
                    size_download=curl.getinfo(pycurl.SIZE_DOWNLOAD_T))
            return code,metrics
        finally:
            if code or not self.reuse_connections:
                curl.close()
            else:
                with self.lock:self.idle[key].append(curl)

    def close(self):
        with self.lock:
            handles=[curl for group in self.idle.values() for curl in group]
            self.idle.clear()
        for curl in handles:
            if not curl.closed:curl.close()
