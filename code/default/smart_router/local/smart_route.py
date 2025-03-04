import time
import socket
import struct
import io
import ssl

try:
    from urllib.parse import urlparse
except ImportError:
    from urlparse import urlparse

import utils
import simple_http_server
from .socket_wrap import SocketWrap
from . import global_var as g
import socks

from xlog import getLogger
xlog = getLogger("smart_router")


SO_ORIGINAL_DST = 80


fake_host = ""


class DontFakeCA(Exception):
    pass


class ConnectFail(Exception):
    pass


class RedirectHttpsFail(Exception):
    pass


class SniNotExist(Exception):
    pass


class NotSupported(Exception):
    def __init__(self, req, sock):
        self.req = req
        self.sock = sock


class SslWrapFail(Exception):
    pass


class XTunnelNotRunning(Exception):
    pass


def is_gae_workable():
    if not g.gae_proxy:
        return False

    return g.gae_proxy.apis.is_workable()


def is_ipv6_ok():
    if not g.gae_proxy:
        return False

    return g.gae_proxy.check_local_network.IPv6.is_ok()


def is_x_tunnel_workable():
    if not g.x_tunnel:
        return False

    return g.x_tunnel.apis.is_workable()


def is_clienthello(data):
    if len(data) < 20:
        return False
    if data.startswith(b'\x16\x03'):
        # TLSv12/TLSv11/TLSv1/SSLv3
        length, = struct.unpack('>h', data[3:5])
        return len(data) == 5 + length
    elif data[0] == '\x80' and data[2:4] == '\x01\x03':
        # SSLv23
        return len(data) == 2 + ord(data[1])
    else:
        return False


def have_ipv6(ips):
    for ip in ips:
        if ":" in ip:
            return True

    return False


def extract_sni_name(packet):
    if not packet.startswith(b'\x16\x03'):
        return

    stream = io.BytesIO(packet)
    stream.read(0x2b)
    session_id_length = ord(stream.read(1))
    stream.read(session_id_length)
    cipher_suites_length, = struct.unpack('>h', stream.read(2))
    stream.read(cipher_suites_length+2)
    extensions_length, = struct.unpack('>h', stream.read(2))
    # extensions = {}
    while True:
        data = stream.read(2)
        if not data:
            break
        etype, = struct.unpack('>h', data)
        elen, = struct.unpack('>h', stream.read(2))
        edata = stream.read(elen)
        if etype == 0:
            server_name = edata[5:]
            return server_name


def netloc_to_host_port(netloc, default_port=80):
    if b":" in netloc:
        host, _, port = netloc.rpartition(':')
        port = int(port)
    else:
        host = netloc
        port = default_port
    return host, port


def get_sni(sock, left_buf=b""):
    if left_buf:
        leadbyte = left_buf[0]
    else:
        leadbyte = sock.recv(1, socket.MSG_PEEK)

    if leadbyte in (b'\x80', b'\x16'):
        if leadbyte == b'\x16':
            for _ in range(2):
                leaddata = left_buf + sock.recv(1024, socket.MSG_PEEK)
                if is_clienthello(leaddata):
                    try:
                        server_name = extract_sni_name(leaddata)
                        return server_name
                    except:
                        break

        raise SniNotExist

    elif leadbyte not in [b"G", b"P", b"D", b"O", b"H", b"T"]:
        raise SniNotExist

    leaddata = b""
    for _ in range(2):
        leaddata = left_buf + sock.recv(65535, socket.MSG_PEEK)
        if leaddata:
            break
        else:
            time.sleep(0.1)
            continue
    if not leaddata:
        raise SniNotExist

    n1 = leaddata.find(b"\r\n")
    if n1 <= -1:
        raise SniNotExist

    req_line = leaddata[:n1]
    words = req_line.split()
    if len(words) == 3:
        method, url, http_version = words
    elif len(words) == 2:
        method, url = words
        http_version = b"HTTP/1.1"
    else:
        raise SniNotExist

    support_methods = tuple([b"GET", b"POST", b"HEAD", b"PUT", b"DELETE", b"PATCH"])
    if method not in support_methods:
        raise SniNotExist

    n2 = leaddata.find(b"\r\n\r\n", n1)
    if n2 <= -1:
        raise SniNotExist
    header_block = leaddata[n1+2:n2]

    lines = header_block.split(b"\r\n")
    # path = url
    host = None
    for line in lines:
        key, _, value = line.rpartition(b":")
        value = value.strip()
        if key.lower() == b"host":
            host, port = netloc_to_host_port(value)
            break
    if host is None or host == b"":
        raise SniNotExist

    return host


def do_direct(sock, host, ips, port, client_address, left_buf=b""):
    remote_sock = g.connect_manager.get_conn(host, ips, port)
    if not remote_sock:
        raise ConnectFail()

    xlog.debug("host:%s:%d direct connect %s success", host, port, remote_sock.ip)
    if left_buf:
        remote_sock.send(left_buf)
    g.pipe_socks.add_socks(sock, remote_sock)


def do_redirect_https(sock, host, ips, port, client_address, left_buf=b""):
    remote_sock = g.connect_manager.get_conn(host, ips, 443)
    if not remote_sock:
        raise RedirectHttpsFail()

    try:
        ssl_sock = ssl.wrap_socket(remote_sock._sock)
    except Exception as e:
        raise RedirectHttpsFail()

    xlog.debug("host:%s:%d redirect_https connect %s success", host, port, remote_sock.ip)

    if left_buf:
        ssl_sock.send(left_buf)
    sw = SocketWrap(ssl_sock, remote_sock.ip, port, host)
    sock.recved_times = 3
    g.pipe_socks.add_socks(sock, sw)


def do_socks(sock, host, port, client_address, left_buf=b""):
    if not g.x_tunnel:
        raise XTunnelNotRunning()

    try:
        conn_id = g.x_tunnel.proxy_session.create_conn(sock, host, port)
    except Exception as e:
        xlog.warn("do_sock to %s:%d, x_tunnel fail:%r", host, port, e)
        raise XTunnelNotRunning()

    if not conn_id:
        xlog.warn("x_tunnel create conn fail")
        raise XTunnelNotRunning()

    # xlog.debug("do_socks %r connect to %s:%d conn_id:%d", client_address, host, port, conn_id)
    if left_buf:
        g.x_tunnel.global_var.session.conn_list[conn_id].transfer_received_data(left_buf)
    g.x_tunnel.global_var.session.conn_list[conn_id].start(block=True)


def do_unwrap_socks(sock, host, port, client_address, req, left_buf=b""):
    if not g.x_tunnel:
        return

    try:
        remote_sock = socks.create_connection(
            (host, port),
            proxy_type="socks5h", proxy_addr="127.0.0.1", proxy_port=g.x_tunnel_socks_port, timeout=15
        )
    except Exception as e:
        xlog.warn("do_unwrap_socks connect to x-tunnel for %s:%d proxy fail.", host, port)
        return

    if isinstance(req.connection, ssl.SSLSocket):
        try:
            # TODO: send SNI
            remote_ssl_sock = ssl.wrap_socket(remote_sock)
        except:
            xlog.warn("do_unwrap_socks ssl_wrap for %s:%d proxy fail.", host, port)
            return
    else:
        remote_ssl_sock = remote_sock

    # avoid close by req.__del__
    req.rfile._close = False
    req.wfile._close = False
    req.connection = None

    if not isinstance(sock, SocketWrap):
        sock = SocketWrap(sock, client_address[0], client_address[1])

    xlog.info("host:%s:%d do_unwrap_socks", host, port)

    remote_ssl_sock.send(left_buf)
    sw = SocketWrap(remote_ssl_sock, "x-tunnel", port, host)
    sock.recved_times = 3
    g.pipe_socks.add_socks(sock, sw)


def do_gae(sock, host, port, client_address, left_buf=""):
    if not g.gae_proxy:
        raise DontFakeCA()

    sock.setblocking(1)
    if left_buf:
        schema = b"http"
    else:
        leadbyte = sock.recv(1, socket.MSG_PEEK)
        if leadbyte in (b'\x80', b'\x16'):
            if host != fake_host and not g.config.enable_fake_ca:
                raise DontFakeCA()

            try:
                sock._sock = g.gae_proxy.proxy_handler.wrap_ssl(sock._sock, host, port, client_address)
            except Exception as e:
                raise SslWrapFail()

            schema = b"https"
        else:
            schema = b"http"

    sock.setblocking(1)
    xlog.debug("host:%s:%d do gae", host, port)
    req = g.gae_proxy.proxy_handler.GAEProxyHandler(sock._sock, client_address, None, xlog)
    req.parse_request()

    if req.path[0] == b'/':
        url = b'%s://%s%s' % (schema, req.headers[b'Host'], req.path)
    else:
        url = req.path

    if url in [b"http://www.twitter.com/xxnet",
                    b"https://www.twitter.com/xxnet",
                    b"http://www.deja.com/xxnet",
                    b"https://www.deja.com/xxnet"
                    ]:
        # for web_ui status page
        # auto detect browser proxy setting is work
        xlog.debug("CONNECT %s %s", req.command, req.path)
        req.wfile.write(req.self_check_response_data)
        return

    if req.upgrade == b"websocket":
        xlog.debug("gae %s not support WebSocket", req.path)
        raise NotSupported(req, sock)

    if len(req.path) >= 2048:
        xlog.debug("gae %s path len exceed 1024 limit", req.path)
        raise NotSupported(req, sock)

    if req.command not in req.gae_support_methods:
        xlog.debug("gae %s %s, method not supported", req.command, req.path)
        raise NotSupported(req, sock)

    req.parsed_url = urlparse(req.path)
    req.do_METHOD()


def try_loop(scense, rule_list, sock, host, port, client_address, left_buf=""):

    start_time = time.time()

    for rule in rule_list:
        try:
            if rule == "redirect_https":
                continue

                # if port != 80:
                #     continue
                #
                # if is_ipv6_ok():
                #     query_type = None
                # else:
                #     query_type = 1
                # ips = g.dns_query.query(host, query_type)
                #
                # do_redirect_https(sock, host, ips, port, client_address, left_buf)
                # xlog.info("%s %s:%d redirect_https", scense, host, port)
                # return

            elif rule == "direct":
                if is_ipv6_ok():
                    query_type = None
                else:
                    query_type = 1
                ips = g.dns_query.query(host, query_type)
                if not ips:
                    continue

                do_direct(sock, host, ips, port, client_address, left_buf)
                xlog.info("%s %s:%d forward to direct", scense, host, port)
                return

            elif rule == "direct6":
                if not is_ipv6_ok():
                    continue

                ips = g.dns_query.query(host, 28)
                if not ips:
                    continue
                    
                do_direct(sock, host, ips, port, client_address, left_buf)
                xlog.info("%s %s:%d forward to direct6", scense, host, port)
                return

            elif rule == "gae":
                if not is_gae_workable() and host != fake_host:
                    # xlog.debug("%s gae host:%s:%d, but gae not work", scense, host, port)
                    continue

                if not g.domain_cache.accept_gae(host):
                    continue

                try:
                    # sni_host = get_sni(sock, left_buf)
                    # xlog.info("%s %s:%d try gae", scense, host, port)
                    do_gae(sock, host, port, client_address, left_buf)
                    return
                except DontFakeCA:
                    continue
                except NotSupported as e:
                    req = e.req
                    left_bufs = [req.raw_requestline]
                    for k in req.headers:
                        v = req.headers[k]
                        left_bufs.append(b"%s: %s\r\n" % (k, v))
                    left_bufs.append(b"\r\n")
                    left_buf = b"".join(left_bufs)

                    return do_unwrap_socks(e.sock, host, port, client_address, req, left_buf=left_buf)
                except SniNotExist:
                    xlog.debug("%s domain:%s get sni fail", scense, host)
                    continue
                except (SslWrapFail, simple_http_server.ParseReqFail) as e:
                    xlog.warn("%s domain:%s fail:%r", scense, host, e)
                    g.domain_cache.report_gae_deny(host, port)
                    sock.close()
                    return
                except simple_http_server.GetReqTimeout:
                    # Happen sometimes, don't known why.
                    xlog.debug("%s host:%s:%d try gae, GetReqTimeout:%d", scense, host, port,
                             (time.time() - start_time) * 1000)
                    sock.close()
                    return
                except Exception as e:
                    xlog.exception("%s host:%s:%d rule:%s except:%r", scense, host, port, rule, e)
                    g.domain_cache.report_gae_deny(host, port)
                    sock.close()
                    return

            elif rule == "socks":
                if not g.x_tunnel or not g.x_tunnel.proxy_session.login_process():
                    continue

                xlog.info("%s %s:%d forward to socks", scense, host, port)
                do_socks(sock, host, port, client_address, left_buf)
                return
            elif rule == "black":
                xlog.info("%s to:%s:%d black", scense, host, port)
                sock.close()
                return
            else:
                xlog.error("%s %s:%d rule:%s unknown", scense, host, port, host, rule)
                sock.close()
                return
        except Exception as e:
            xlog.exception("%s %s to %s:%d except:%r", scense, rule, host, port, e)

    xlog.info("%s %s to %s:%d fail", scense, rule_list, host, port)
    sock.close()
    return


def handle_ip_proxy(sock, ip, port, client_address):
    xlog.debug("connect to %s:%d from:%s:%d", ip, port, client_address[0], client_address[1])

    if not isinstance(sock, SocketWrap):
        sock = SocketWrap(sock, client_address[0], client_address[1])

    rule = g.user_rules.check_host(ip, port)
    if not rule:
        if utils.is_private_ip(ip):
            rule = "direct"

    if rule:
        return try_loop("ip user", [rule], sock, ip, port, client_address)

    try:
        host = get_sni(sock)
        if host:
            return handle_domain_proxy(sock, host, port, client_address)
    except SniNotExist as e:
        xlog.debug("ip:%s:%d get sni fail", ip, port)

    record = g.ip_cache.get(ip)
    if record and record["r"] != "unknown":
        rule = record["r"]
        if rule == "gae":
            rule_list = ["gae", "socks", "direct"]
        elif rule == "socks":
            rule_list = ["socks", "gae", "direct"]
        else:
            rule_list = ["direct", "gae", "socks"]
    elif g.ip_region.check_ip(ip):
        rule_list = ["direct", "socks"]
    else:
        rule_list = ["direct", "gae", "socks"]

    if not g.config.auto_direct:
        for rule in ["direct", "redirect_https"]:
            try:
                rule_list.remove(rule)
            except:
                pass
    elif g.config.auto_direct6 and "direct" in rule_list:
        rule_list.insert(rule_list.index("direct"), "direct6")

    if not g.config.enable_fake_ca and port == 443 or not g.config.auto_gae:
        try:
            rule_list.remove("gae")
        except:
            pass

    try_loop("ip", rule_list, sock, ip, port, client_address)


def handle_domain_proxy(sock, host, port, client_address, left_buf=""):
    global fake_host

    if not fake_host and g.gae_proxy:
        fake_host = g.gae_proxy.web_control.get_fake_host()

    if not isinstance(sock, SocketWrap):
        sock = SocketWrap(sock, client_address[0], client_address[1])

    # Check user rules
    sock.target = "%s:%d" % (host, port)
    rule = g.user_rules.check_host(host, port)
    if not rule:
        if host == fake_host:
            rule = "gae"
        elif utils.check_ip_valid(host) and utils.is_private_ip(host):
            rule = "direct"

    if rule:
        return try_loop("domain user", [rule], sock, host, port, client_address, left_buf)

    if g.config.block_advertisement and g.gfwlist.is_advertisement(host):
        xlog.info("block advertisement %s:%d", host, port)
        sock.close()
        return

    #ips = g.dns_query.query(host)
    #if check_local_network.IPv6.is_ok() and have_ipv6(ips) and port == 443:
    #    rule_list = ["direct", "gae", "socks", "redirect_https"]
    # gae is more faster then direct.

    rule = g.domain_cache.get_rule(host)
    if rule != "unknown":
        if rule == "gae":
            rule_list = ["gae", "socks", "redirect_https", "direct"]
        elif rule == "socks":
            rule_list = ["socks", "gae", "redirect_https", "direct"]
        else:
            rule_list = ["direct", "gae", "socks", "redirect_https"]

        if not g.domain_cache.accept_gae(host):
            rule_list.remove("gae")
    elif g.config.country_code == "CN":
        if g.gfwlist.in_white_list(host):
            rule_list = ["direct", "gae", "socks", "redirect_https"]
        elif g.gfwlist.in_block_list(host):
            if g.config.pac_policy == "black_X-Tunnel":
                rule_list = ["socks", "redirect_https", "direct", "gae"]
            else:
                rule_list = ["gae", "socks", "redirect_https", "direct"]
        else:
            ips = g.dns_query.query_recursively(host, 1)
            if g.ip_region.check_ips(ips):
                rule_list = ["direct", "socks", "redirect_https"]
            else:
                rule_list = ["direct", "gae", "socks", "redirect_https"]
    else:
        rule_list = ["direct", "socks", "gae", "redirect_https"]

    # check config.
    if not g.config.auto_direct:
        for rule in ["direct", "redirect_https"]:
            try:
                rule_list.remove(rule)
            except:
                pass
    elif g.config.auto_direct6 and "direct" in rule_list:
        rule_list.insert(rule_list.index("direct"), "direct6")

    if not g.config.enable_fake_ca and port == 443 or not g.config.auto_gae:
        try:
            rule_list.remove("gae")
        except:
            pass

    xlog.debug("connect to %s:%d from:%s:%d, rule:%s", host, port, client_address[0], client_address[1],
               utils.to_str(rule_list))
    try_loop("domain", rule_list, sock, host, port, client_address, left_buf)
