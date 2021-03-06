# -*- coding: utf-8 -*-
import os
import ssl
import urlparse

from lib.requests_toolbelt.adapters import host_header_ssl
# from lib import cloudscraper
from lib import doh
from platformcode import logger, config
import requests

try:
    import _sqlite3 as sql
except:
    import sqlite3 as sql

db = os.path.join(config.get_data_path(), 'kod_db.sqlite')


class CustomSocket(ssl.SSLSocket):
    def __init__(self, *args, **kwargs):
        super(CustomSocket, self).__init__(*args, **kwargs)

class CustomContext(ssl.SSLContext):
    def __init__(self, protocol, hostname, *args, **kwargs):
        self.hostname = hostname
        super(CustomContext, self).__init__(protocol)

    def wrap_socket(self, sock, server_side=False,
                    do_handshake_on_connect=True,
                    suppress_ragged_eofs=True,
                    server_hostname=None):
        return CustomSocket(sock=sock, server_side=server_side,
                         do_handshake_on_connect=do_handshake_on_connect,
                         suppress_ragged_eofs=suppress_ragged_eofs,
                         server_hostname=self.hostname,
                         _context=self)


class CipherSuiteAdapter(host_header_ssl.HostHeaderSSLAdapter):

    def __init__(self, hostname, *args, **kwargs):
        self.ssl_context = kwargs.pop('ssl_context', None)
        self.cipherSuite = kwargs.pop('cipherSuite', None)
        self.hostname = hostname

        if not self.ssl_context:
            self.ssl_context = CustomContext(ssl.PROTOCOL_TLS, hostname)
            self.ssl_context.set_ciphers(self.cipherSuite)

        super(CipherSuiteAdapter, self).__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs['ssl_context'] = self.ssl_context
        return super(CipherSuiteAdapter, self).init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs['ssl_context'] = self.ssl_context
        return super(CipherSuiteAdapter, self).proxy_manager_for(*args, **kwargs)


class session(requests.Session):
    def __init__(self, *args, **kwargs):
        self.conn = sql.connect(db)
        self.cur = self.conn.cursor()
        super(session, self).__init__(*args, **kwargs)

    def getIp(self, domain):
        ip = None
        try:
            self.cur.execute('select ip from dnscache where domain=?', (domain,))
            ip = self.cur.fetchall()[0][0]
            logger.info('Cache DNS: ' + domain + ' = ' + str(ip))
        except:
            pass
        if not ip:  # not cached
            try:
                ip = doh.query(domain)[0]
                logger.info('Query DoH: ' + domain + ' = ' + str(ip))
                self.writeToCache(domain, ip)
            except Exception:
                logger.error('Failed to resolve hostname, fallback to normal dns')
                import traceback
                logger.error(traceback.print_exc())
                ip = domain
        return ip

    def writeToCache(self, domain, ip):
        try:
            self.cur.execute('insert into dnscache values(?,?)', (domain, ip))
        except:
            self.cur.execute("""CREATE TABLE IF NOT EXISTS dnscache(
                    "domain"	TEXT NOT NULL UNIQUE,
                    "ip"	TEXT NOT NULL,
                    PRIMARY KEY("domain")
                );""")
        self.conn.commit()

    def flushDns(self, method, realUrl, domain, **kwargs):
        self.cur.execute('delete from dnscache where domain=?', (domain,))
        self.conn.commit()
        return self.request(method, realUrl, flushedDns=True, **kwargs)

    def request(self, method, url, headers=None, flushedDns=False, **kwargs):
        try:
            parse = urlparse.urlparse(url)
        except:
            raise requests.exceptions.InvalidURL
        if parse.netloc:
            domain = parse.netloc
        else:
            raise requests.exceptions.URLRequired
        ip = self.getIp(domain)
        self.mount('https://', CipherSuiteAdapter(domain, cipherSuite='ALL'))
        realUrl = url

        if headers:
            headers["Host"] = domain
        else:
            headers = {"Host": domain}
        ret = None
        tryFlush = False

        parse = list(parse)
        parse[1] = ip
        url = urlparse.urlunparse(parse)

        allow_redirects = kwargs.get('allow_redirects', True)
        if 'allow_redirects' in kwargs:
            del kwargs['allow_redirects']
        try:
            ret = super(session, self).request(method, url, headers=headers, allow_redirects=False, **kwargs)
            newUrl = urlparse.urlparse(ret.headers.get('Location', realUrl))
            if not newUrl.netloc and not newUrl.scheme:
                newUrl = list(newUrl)
                newUrl[0] = 'https://'
                newUrl[1] = domain
            newUrl = urlparse.urlunparse(newUrl)
            if allow_redirects:
                redirectN = 0
                while newUrl != realUrl and redirectN < self.max_redirects:
                    ret = self.request(method, newUrl, headers=headers, **kwargs)
                    newUrl = ret.headers.get('Location', realUrl)
                    redirectN += 1
            ret.url = newUrl
        except Exception as e:
            logger.info('Request for ' + domain + ' with ip ' + ip + ' failed')
            logger.info(e)
            tryFlush = True
        if (tryFlush or not ret) and not flushedDns:  # re-request ips and update cache
            logger.info('Flushing dns cache for ' + domain)
            return self.flushDns(method, realUrl, domain, **kwargs)

        if not ret:
            raise requests.exceptions.RequestException
        return ret
