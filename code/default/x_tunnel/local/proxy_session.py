import time
import json
import threading
import xstruct as struct

from xlog import getLogger
xlog = getLogger("x_tunnel")

import utils
from . import base_container
import encrypt
from . import global_var as g
from gae_proxy.local import check_local_network


def encrypt_data(data):
    if g.config.encrypt_data:
        return encrypt.Encryptor(g.config.encrypt_password, g.config.encrypt_method).encrypt(data)
    else:
        return data


def decrypt_data(data):
    if g.config.encrypt_data:
        if isinstance(data, memoryview):
            data = data.tobytes()
        return encrypt.Encryptor(g.config.encrypt_password, g.config.encrypt_method).decrypt(data)
    else:
        return data


def sleep(t):
    end_time = time.time() + t
    while g.running:
        if time.time() > end_time:
            return

        sleep_time = min(1, end_time - time.time())
        time.sleep(sleep_time)


class ProxySession(object):
    def __init__(self):
        self.wait_queue = base_container.WaitQueue()
        self.send_buffer = base_container.SendBuffer(max_payload=g.config.max_payload)
        self.receive_process = base_container.BlockReceivePool(self.download_data_processor)
        self.lock = threading.Lock()  # lock for conn_id, sn generation, on_road_num change,

        self.send_delay = g.config.send_delay / 1000.0
        self.ack_delay = g.config.ack_delay / 1000.0
        self.resend_timeout = g.config.resend_timeout / 1000.0

        self.running = False
        self.round_trip_thread = {}
        self.session_id = utils.generate_random_lowercase(8)
        self.last_conn_id = 0
        self.last_transfer_no = 0
        self.conn_list = {}
        self.transfer_list = {}
        self.on_road_num = 0
        self.last_receive_time = 0
        self.last_send_time = 0
        self.traffic = 0
        self.server_send_buf_size = 0
        self.target_on_roads = 0

        # the receive time of the tail of the socket receive buffer
        # if now - oldest_received_time > delay, then send.
        # set only no data in receive buffer
        # if no data left, set to 0
        self.oldest_received_time = 0

        self.last_state = {
            "timeout": 0,
        }
        if g.config.enable_tls_relay:
            threading.Thread(target=self.reporter).start()

    def start(self):
        with self.lock:
            if self.running is True:
                xlog.warn("session try to run but is running.")
                return True

            self.round_trip_thread = {}

            self.session_id = utils.generate_random_lowercase(8)
            self.last_conn_id = 0
            self.last_transfer_no = 0
            self.conn_list = {}
            self.transfer_list = {}
            self.last_send_time = time.time()
            self.on_road_num = 0
            self.last_receive_time = 0
            self.traffic = 0

            # sn => (payload, send_time)
            # sn => ack
            self.wait_ack_send_list = dict()
            self.ack_send_continue_sn = 0

            self.received_sn = []
            self.receive_next_sn = 1
            self.target_on_roads = 0

            if not self.login_session():
                xlog.warn("x-tunnel login_session fail, session not start")
                return False

            self.running = True

            for i in range(0, g.config.concurent_thread_num):
                self.round_trip_thread[i] = threading.Thread(target=self.normal_round_trip_worker, args=(i,))
                self.round_trip_thread[i].daemon = True
                self.round_trip_thread[i].start()

            threading.Thread(target=self.timer).start()
            xlog.info("session started.")
            return True

    def stop(self):
        if not self.running:
            xlog.warn("session stop but not running")
            return

        with self.lock:
            self.running = False
            self.target_on_roads = 0
            for i in range(0, g.config.concurent_thread_num):
                self.wait_queue.notify()

            self.session_id = ""
            self.close_all_connection()

            self.send_buffer.reset()
            self.receive_process.reset()
            self.wait_queue.stop()

            xlog.debug("session stopped.")

    def reset(self):
        xlog.debug("session reset")
        self.stop()
        return self.start()

    def is_idle(self):
        return time.time() - self.last_send_time > 60

    def timer(self):
        while self.running:
            if self.send_buffer.pool_size > 0 and time.time() - self.oldest_received_time > self.send_delay:
                self.wait_queue.notify()
            time.sleep(self.send_delay)

    def reporter(self):
        sleep(5)
        while g.running:
            if not g.running:
                break

            self.check_report_status()
            sleep(g.config.report_interval)

    def check_report_status(self):
        if self.is_idle():
            return

        good_ip_num = 0
        for ip in g.tls_relay_front.ip_manager.ip_dict:
            ip_state = g.tls_relay_front.ip_manager.ip_dict[ip]
            fail_times = ip_state["fail_times"]
            if fail_times == 0:
                good_ip_num += 1
        if good_ip_num:
            return

        stat = self.get_stat("minute")
        stat["version"] = g.xxnet_version
        stat["client_uuid"] = g.client_uuid
        stat["global"]["timeout"] = g.stat["timeout_roundtrip"] - self.last_state["timeout"]
        stat["global"]["ipv6"] = check_local_network.IPv6.is_ok()
        stat["tls_relay_front"]["ip_dict"] = g.tls_relay_front.ip_manager.ip_dict

        report_dat = {
            "account": str(g.config.login_account),
            "password": str(g.config.login_password),
            "stat": stat,
        }
        xlog.debug("start report_stat")
        status, info = call_api("/report_stat", report_dat)
        if not status:
            xlog.warn("report fail.")
            return

        self.last_state["timeout"] = g.stat["timeout_roundtrip"]
        data = info["data"]
        g.tls_relay_front.set_ips(data["ips"])

    @staticmethod
    def get_stat(type="second"):
        def convert(num, units=('B', 'KB', 'MB', 'GB')):
            for unit in units:
                if num >= 1024:
                    num /= 1024.0
                else:
                    break
            return '{:.1f} {}'.format(num, unit)

        res = {}
        rtt = 0
        recent_sent = 0
        recent_received = 0
        total_sent = 0
        total_received = 0
        for front in g.http_client.all_fronts:
            if not front:
                continue
            name = front.name
            dispatcher = front.get_dispatcher()
            if not dispatcher:
                res[name] = {
                    "score": "False",
                    "rtt": 9999,
                    "success_num": 0,
                    "fail_num": 0,
                    "worker_num": 0,
                    "total_traffics": "Up: 0 / Down: 0"
                }
                continue
            score = dispatcher.get_score()
            if score is None:
                score = "False"
            else:
                score = int(score)

            if type == "second":
                stat = dispatcher.second_stat
            elif type == "minute":
                stat = dispatcher.minute_stat
            else:
                raise Exception()

            rtt = max(rtt, stat["rtt"])
            recent_sent += stat["sent"]
            recent_received += stat["received"]
            total_sent += dispatcher.total_sent
            total_received += dispatcher.total_received
            res[name] = {
                "score": score,
                "rtt": stat["rtt"],
                "success_num": dispatcher.success_num,
                "fail_num": dispatcher.fail_num,
                "worker_num": dispatcher.worker_num(),
                "total_traffics": "Up: %s / Down: %s" % (
                    convert(dispatcher.total_sent), convert(dispatcher.total_received))
            }

        res["global"] = {
            "handle_num": g.socks5_server.handler.handle_num,
            "rtt": int(rtt),
            "roundtrip_num": g.stat["roundtrip_num"],
            "slow_roundtrip": g.stat["slow_roundtrip"],
            "timeout_roundtrip": g.stat["timeout_roundtrip"],
            "resend": g.stat["resend"],
            "speed": "Up: %s/s / Down: %s/s" % (convert(recent_sent), convert(recent_received)),
            "total_traffics": "Up: %s / Down: %s" % (convert(total_sent), convert(total_received))
        }
        return res

    def status(self):
        out_string = "session_id:%s<br>\n" % self.session_id

        out_string += "running:%d<br>\n" % self.running
        out_string += "last_send_time:%f<br>\n" % (time.time() - self.last_send_time)
        out_string += "last_receive_time:%f<br>\n" % (time.time() - self.last_receive_time)
        out_string += "last_conn:%d<br>\n" % self.last_conn_id
        out_string += "last_transfer_no:%d<br>\n" % self.last_transfer_no
        out_string += "traffic:%d<br>\n" % self.traffic

        out_string += "on_road_num:%d<br>\n" % self.on_road_num
        out_string += "transfer_list: %d<br>\r\n" % len(self.transfer_list)
        for transfer_no in sorted(self.transfer_list.keys()):
            transfer = self.transfer_list[transfer_no]
            if "start" in self.transfer_list[transfer_no]:
                time_way = " t:" + str((time.time() - self.transfer_list[transfer_no]["start"]))
            else:
                time_way = ""
            out_string += "[%d] %s %s<br>\r\n" % (transfer_no, json.dumps(transfer), time_way)

        out_string += "<br>\n" + self.wait_queue.status()
        out_string += "<br>\n" + self.send_buffer.status()
        out_string += "<br>\n" + self.receive_process.status()

        for conn_id in self.conn_list:
            out_string += "<br>\n" + self.conn_list[conn_id].status()

        return out_string

    def login_session(self):
        if len(g.server_host) == 0:
            return False

        start_time = time.time()
        while time.time() - start_time < 30:
            try:
                magic = b"P"
                pack_type = 1
                upload_data_head = struct.pack("<cBB8sIHIIHH", magic, g.protocol_version, pack_type,
                                               bytes(self.session_id),
                                               g.config.max_payload, g.config.send_delay, g.config.windows_size,
                                               int(g.config.windows_ack), g.config.resend_timeout, g.config.ack_delay)
                upload_data_head += struct.pack("<H", len(g.config.login_account)) + utils.to_bytes(g.config.login_account)
                upload_data_head += struct.pack("<H", len(g.config.login_password)) + utils.to_bytes(g.config.login_password)

                upload_post_data = encrypt_data(upload_data_head)

                content, status, response = g.http_client.request(method="POST", host=g.server_host, path="/data",
                                                                  data=upload_post_data,
                                                                  timeout=g.config.network_timeout)

                time_cost = time.time() - start_time

                if status == 521:
                    g.last_api_error = "session server is down."
                    xlog.warn("login session server is down, try get new server.")
                    g.server_host = None
                    return False

                if status != 200:
                    g.last_api_error = "session server login fail:%r" % status
                    xlog.warn("login session fail, status:%r", status)
                    continue

                if len(content) < 6:
                    g.last_api_error = "session server protocol fail, login res len:%d" % len(content)
                    xlog.error("login data len:%d fail", len(content))
                    continue

                info = decrypt_data(content)
                magic, protocol_version, pack_type, res, message_len = struct.unpack("<cBBBH", info[:6])
                message = info[6:]
                if isinstance(message, memoryview):
                    message = message.tobytes()

                if magic != b"P" or protocol_version != g.protocol_version or pack_type != 1:
                    xlog.error("login_session time:%d head error:%s", 1000 * time_cost, utils.str2hex(info[:6]))
                    return False

                if res != 0:
                    g.last_api_error = "session server login fail, code:%d msg:%s" % (res, message)
                    xlog.warn("login_session time:%d fail, res:%d msg:%s", 1000 * time_cost, res, message)
                    return False

                g.last_api_error = ""
                xlog.info("login_session %s time:%d msg:%s", self.session_id, 1000 * time_cost, message)
                return True
            except Exception as e:
                xlog.exception("login_session e:%r", e)
                time.sleep(1)

        return False

    def create_conn(self, sock, host, port):
        if not self.running:
            xlog.debug("session not running, try to connect")
            return None

        self.target_on_roads = max(g.config.min_on_road, self.target_on_roads)

        self.lock.acquire()
        self.last_conn_id += 2
        conn_id = self.last_conn_id
        self.lock.release()
        if isinstance(host, str):
            host = host.encode("ascii")

        seq = 0
        cmd_type = 0  # create connection
        sock_type = 0  # TCP
        data = struct.pack("<IBBH", seq, cmd_type, sock_type, len(host)) + host + struct.pack("<H", port)
        self.send_conn_data(conn_id, data)

        self.conn_list[conn_id] = base_container.Conn(self, conn_id, sock, host, port, g.config.windows_size,
                                                      g.config.windows_ack, True, xlog)
        return conn_id

    # Called by stop
    def close_all_connection(self):
        xlog.info("start close all connection")
        conn_list = dict(self.conn_list)
        for conn_id in conn_list:
            try:
                # xlog.debug("stopping conn:%d", conn_id)
                self.conn_list[conn_id].stop(reason="system reset")
            except Exception as e:
                xlog.warn("stopping conn:%d fail:%r", conn_id, e)
                pass
        # self.conn_list = {}
        xlog.debug("stop all connection finished")

    def remove_conn(self, conn_id):
        xlog.debug("remove conn:%d", conn_id)
        try:
            del self.conn_list[conn_id]
        except:
            pass

        if len(self.conn_list) == 0:
            self.target_on_roads = 0

    def send_conn_data(self, conn_id, data):
        if not self.running:
            xlog.warn("send_conn_data but not running")
            return

        # xlog.debug("upload conn:%d, len:%d", conn_id, len(data))
        buf = base_container.WriteBuffer()
        buf.append(struct.pack("<II", conn_id, len(data)))
        buf.append(data)
        self.send_buffer.put(buf)

        if self.oldest_received_time == 0:
            self.oldest_received_time = time.time()
        elif self.send_buffer.pool_size > g.config.max_payload or \
                time.time() - self.oldest_received_time > self.send_delay:
            # xlog.debug("notify on send conn data")
            self.wait_queue.notify()

    @staticmethod
    def sn_payload_head(sn, payload):
        return struct.pack("<II", sn, len(payload))

    def get_data(self, work_id):
        time_now = time.time()
        buf = base_container.WriteBuffer()

        with self.lock:
            for sn in self.wait_ack_send_list:
                pk = self.wait_ack_send_list[sn]
                if isinstance(pk, str):
                    continue

                payload, send_time = pk
                if time_now - send_time > self.resend_timeout:
                    g.stat["resend"] += 1
                    buf.append(self.sn_payload_head(sn, payload))
                    buf.append(payload)
                    self.wait_ack_send_list[sn] = (payload, time_now)
                    if len(buf) > g.config.max_payload:
                        return buf

            if self.send_buffer.pool_size > g.config.max_payload or \
                    (self.send_buffer.pool_size > 0 and
                     (time.time() - self.oldest_received_time > self.send_delay or work_id < self.target_on_roads)):
                payload, sn = self.send_buffer.get()
                self.wait_ack_send_list[sn] = (payload, time_now)
                buf.append(self.sn_payload_head(sn, payload))
                buf.append(payload)

                if self.send_buffer.pool_size == 0:
                    self.oldest_received_time = 0

                if len(buf) > g.config.max_payload:
                    return buf

        return buf

    def get_ack(self, force=False):
        time_now = time.time()
        # xlog.debug("get_ack force:%d, last_receive_time:%f, last_send_time:%f, time_now - self.last_send_time:%f",
        #           force, self.last_receive_time, self.last_send_time, time_now - self.last_send_time)

        if force or \
                (self.last_receive_time > self.last_send_time and
                 time_now - self.last_receive_time > self.ack_delay):

            buf = base_container.WriteBuffer()
            buf.append(struct.pack("<I", self.receive_process.next_sn - 1))
            for sn in self.receive_process.block_list:
                buf.append(struct.pack("<I", sn))
            return buf

        return ""

    def get_send_data(self, work_id):
        force = False
        while self.running:
            data = self.get_data(work_id)
            # xlog.debug("get_send_data work_id:%d len:%d", work_id, len(data))
            if data or work_id < self.target_on_roads:
                # xlog.debug("got data, force get ack")
                force = True

            ack = self.get_ack(force=force)
            if data or ack or force:
                # xlog.debug("get_send_data work_id:%d data_len:%d ack_len:%d force:%d", work_id, len(data), len(ack), force)
                return data, ack

            self.wait_queue.wait(work_id)

        xlog.debug("get_send_data on stop")
        return "", ""

    def ack_process(self, ack):
        self.lock.acquire()
        try:
            last_ack = struct.unpack("<I", ack.get(4))[0]

            while len(ack):
                sn = struct.unpack("<I", ack.get(4))[0]
                # xlog.debug("ack: %d", sn)
                if sn in self.wait_ack_send_list:
                    self.wait_ack_send_list[sn] = "acked"

            for sn in self.wait_ack_send_list:
                if sn > last_ack:
                    continue
                if self.wait_ack_send_list[sn] == "acked":
                    continue

                # xlog.debug("last_ack:%d sn:%d", last_ack, sn)
                self.wait_ack_send_list[sn] = "acked"

            while (self.ack_send_continue_sn + 1) in self.wait_ack_send_list and \
                    self.wait_ack_send_list[self.ack_send_continue_sn + 1] == "acked":
                self.ack_send_continue_sn += 1
                del self.wait_ack_send_list[self.ack_send_continue_sn]

        except Exception as e:
            xlog.exception("ack_process:%r", e)
        finally:
            self.lock.release()

    def download_data_processor(self, data):
        try:
            while len(data):
                conn_id, payload_len = struct.unpack("<II", data.get(8))
                payload = data.get_buf(payload_len)

                # xlog.debug("conn:%d upload data len:%d", conn_id, len(payload))
                if conn_id not in self.conn_list:
                    xlog.debug("conn:%d not exist", conn_id)
                    continue
                self.conn_list[conn_id].put_cmd_data(payload)
        except Exception as e:
            xlog.exception("download_data_processor:%r", e)

    def round_trip_process(self, data, ack):
        while len(data):
            sn, plen = struct.unpack("<II", data.get(8))
            pdata = data.get_buf(plen)
            # xlog.debug("download sn:%d len:%d", sn, plen)

            self.receive_process.put(sn, pdata)

        self.ack_process(ack)

    def get_transfer_no(self):
        with self.lock:
            self.last_transfer_no += 1
            transfer_no = self.last_transfer_no

        return transfer_no

    def trigger_more(self):
        running_num = g.config.concurent_thread_num - len(self.wait_queue.waiters)
        action_num = self.target_on_roads - running_num
        if action_num <= 0:
            return

        for _ in range(0, action_num):
            self.wait_queue.notify()

    def normal_round_trip_worker(self, work_id):
        while self.running:
            data, ack = self.get_send_data(work_id)

            if not self.running:
                return

            send_data_len = len(data)
            send_ack_len = len(ack)
            transfer_no = self.get_transfer_no()
            # xlog.debug("trip:%d no:%d send data:%s", work_id, transfer_no, parse_data(data))

            magic = b"P"
            pack_type = 2

            if self.send_buffer.pool_size > g.config.max_payload or \
                    (self.send_buffer.pool_size and len(self.wait_queue.waiters) < g.config.min_on_road):
                server_timeout = 0
            elif work_id > g.config.concurent_thread_num * 0.9:
                server_timeout = 1
            elif work_id > g.config.concurent_thread_num * 0.7:
                server_timeout = 3
            else:
                server_timeout = g.config.roundtrip_timeout

            request_session_id = self.session_id
            upload_data_head = struct.pack("<cBB8sIBIH", magic, g.protocol_version, pack_type,
                                           bytes(self.session_id), transfer_no,
                                           server_timeout, send_data_len, send_ack_len)
            upload_post_buf = base_container.WriteBuffer(upload_data_head)
            upload_post_buf.append(data)
            upload_post_buf.append(ack)
            upload_post_data = upload_post_buf.to_bytes()
            upload_post_data = encrypt_data(upload_post_data)
            self.last_send_time = time.time()

            sleep_time = 1

            start_time = time.time()

            with self.lock:
                self.on_road_num += 1
                self.transfer_list[transfer_no] = {}
                self.transfer_list[transfer_no]["stat"] = "request"
                self.transfer_list[transfer_no]["start"] = start_time

            # xlog.debug("start trip transfer_no:%d send_data_len:%d ack_len:%d timeout:%d",
            #           transfer_no, send_data_len, send_ack_len, server_timeout)
            try:
                content, status, response = g.http_client.request(method="POST", host=g.server_host,
                                                                  path="/data?tid=%d" % transfer_no,
                                                                  data=upload_post_data,
                                                                  headers={
                                                                      "Content-Length": str(len(upload_post_data))},
                                                                  timeout=server_timeout + g.config.network_timeout)

                traffic = len(upload_post_data) + len(content) + 645
                self.traffic += traffic
                g.quota -= traffic
                if g.quota < 0:
                    g.quota = 0
            except Exception as e:
                if self.running:
                    xlog.exception("request except:%r ", e)

                time.sleep(sleep_time)
                continue
            finally:
                with self.lock:
                    self.on_road_num -= 1
                    try:
                        if transfer_no in self.transfer_list:
                            del self.transfer_list[transfer_no]
                    except:
                        pass

            g.stat["roundtrip_num"] += 1
            roundtrip_time = (time.time() - start_time)

            if status == 521:
                xlog.warn("X-tunnel server is down, try get new server.")
                g.server_host = None
                self.stop()
                login_process()
                return

            if status != 200:
                xlog.warn("roundtrip time:%f transfer_no:%d send:%d status:%r ",
                          roundtrip_time, transfer_no, send_data_len, status)
                time.sleep(sleep_time)
                continue

            recv_len = len(content)
            if recv_len < 6:
                xlog.warn("roundtrip time:%f transfer_no:%d send:%d recv:%d Head",
                          roundtrip_time, transfer_no, send_data_len, recv_len)
                continue

            content = decrypt_data(content)
            payload = base_container.ReadBuffer(content)

            magic, version, pack_type = struct.unpack("<cBB", payload.get(3))
            if magic != b"P" or version != g.protocol_version:
                xlog.warn("get data head:%s", utils.str2hex(content[:2]))
                time.sleep(sleep_time)
                continue

            if pack_type == 3:  # error report
                error_code, message_len = struct.unpack("<BH", payload.get(3))
                message = payload.get(message_len)
                # xlog.warn("report code:%d, msg:%s", error_code, message)
                if error_code == 1:
                    # no quota
                    xlog.warn("x_server error:no quota")
                    self.stop()
                    return
                elif error_code == 2:
                    # unpack error
                    xlog.warn("roundtrip time:%f transfer_no:%d send:%d recv:%d unpack_error:%s",
                              roundtrip_time, transfer_no, send_data_len, len(content), message)
                    continue
                elif error_code == 3:
                    # session not exist
                    if self.session_id == request_session_id:
                        xlog.warn("server session_id:%s not exist, reset session.", request_session_id)
                        self.reset()
                        return
                    else:
                        continue
                else:
                    xlog.error("unknown error code:%d, message:%s", error_code, message)
                    time.sleep(sleep_time)
                    continue

            if pack_type != 2:  # normal download traffic pack
                xlog.error("pack type:%d", pack_type)
                time.sleep(100)
                continue

            time_cost, server_send_pool_size, data_len, ack_len = struct.unpack("<IIIH", payload.get(14))
            xlog.debug(
                "trip:%d no:%d tc:%f cost:%f to:%d snd:%d rcv:%d s_pool:%d on_road:%d target:%d",
                work_id, transfer_no,
                roundtrip_time, time_cost / 1000.0, server_timeout,
                send_data_len, len(content), server_send_pool_size,
                self.on_road_num,
                self.target_on_roads)

            if len(self.conn_list) == 0:
                self.target_on_roads = 0
            elif len(content) >= g.config.max_payload:
                self.target_on_roads = \
                    min(g.config.concurent_thread_num - g.config.min_on_road, self.target_on_roads + 10)
            elif len(content) <= 21:
                self.target_on_roads = max(g.config.min_on_road, self.target_on_roads - 5)
            self.trigger_more()

            rtt = roundtrip_time * 1000 - time_cost
            rtt = max(100, rtt)
            speed = (send_data_len + len(content) + 400) / rtt
            response.worker.update_debug_data(rtt, send_data_len, len(content), speed)
            if rtt > 8000:
                xlog.debug("rtt:%d speed:%d trace:%s", rtt, speed, response.worker.get_trace())
                xlog.debug("task trace:%s", response.task.get_trace())
                g.stat["slow_roundtrip"] += 1

            try:
                data = payload.get_buf(data_len)
                ack = payload.get_buf(ack_len)
            except Exception as e:
                xlog.warn("trip:%d no:%d data not enough %r", work_id, transfer_no, e)
                continue

            # xlog.debug("trip:%d no:%d recv data:%s", work_id, transfer_no, parse_data(data))

            try:
                self.round_trip_process(data, ack)

                self.last_receive_time = time.time()
            except Exception as e:
                xlog.exception("data process:%r", e)

        xlog.info("roundtrip thread exit")


def parse_data(data):
    if len(data) == 0:
        return ""

    o = ""

    data = bytes(data)
    data = base_container.ReadBuffer(data)
    while len(data):

        sn, block_len = struct.unpack("<II", data.get(8))
        block = data.get_buf(block_len)

        o += "sn:%d {" % sn

        while len(block):
            conn_id, payload_len = struct.unpack("<II", block.get(8))

            o += "conn:%d [" % conn_id
            conn_data = block.get_buf(payload_len)

            seq = struct.unpack("<I", conn_data.get(4))[0]
            cmd_id = struct.unpack("<B", conn_data.get(1))[0]
            conn_payload = conn_data.get_buf()
            if cmd_id == 0:  # create connection
                sock_type = struct.unpack("<B", conn_payload.get(1))[0]
                host_len = struct.unpack("<H", conn_payload.get(2))[0]
                host = str(bytes(conn_payload.get(host_len)))
                port = struct.unpack("<H", conn_payload.get(2))[0]
                o += "%d|Connect:%s:%d" % (seq, host, port)
            elif cmd_id == 1:  # data
                o += "%d|D:%d" % (seq, len(conn_payload))
            elif cmd_id == 2:  # closed
                o += "%d|Closed:%s" % (seq, conn_payload)
            elif cmd_id == 3:  # ack
                position = struct.unpack("<Q", conn_payload.get())[0]
                o += "%d|Ack:%d" % (seq, position)

            o += "],"
        o += "},"

    return o


def calculate_quota_left(quota_list):
    time_now = int(time.time())
    quota_left = 0

    try:
        if "current" in quota_list:
            c_q_end_time = quota_list["current"]["end_time"]
            if c_q_end_time > time_now:
                quota_left += quota_list["current"]["quota"]

        if "backup" in quota_list:
            for qt in quota_list["backup"]:
                b_q_quota = qt["quota"]
                b_q_end_time = qt["end_time"]
                if b_q_end_time < time_now:
                    continue

                quota_left += b_q_quota

    except Exception as e:
        xlog.exception("calculate_quota_left %s %r", quota_list, e)

    return quota_left


def call_api(path, req_info):
    if not path.startswith("/"):
        path = "/" + path

    try:
        upload_post_data = json.dumps(req_info)
        upload_post_data = encrypt_data(upload_post_data)

        start_time = time.time()
        while time.time() - start_time < 30:
            content, status, response = g.http_client.request(method="POST", host=g.config.api_server, path=path,
                                                              headers={"Content-Type": "application/json"},
                                                              data=upload_post_data, timeout=5)
            if status >= 400:
                time.sleep(1)
                continue
            else:
                break

        time_cost = time.time() - start_time
        if status != 200:
            reason = "status:%r" % status
            xlog.warn("api:%s fail:%s t:%d", path, reason, time_cost)
            g.last_api_error = reason
            return False, reason

        content = decrypt_data(content)
        if isinstance(content, memoryview):
            content = content.tobytes()

        content = utils.to_str(content)
        try:
            info = json.loads(content)
        except Exception as e:
            g.last_api_error = "parse json fail"
            xlog.warn("api:%s parse json:%s fail:%r", path, content, e)
            return False, "parse json fail"

        res = info["res"]
        if res != "success":
            g.last_api_error = info["reason"]
            xlog.warn("api:%s fail:%s", path, info["reason"])
            return False, info["reason"]

        xlog.info("api:%s success t:%d", path, time_cost * 1000)
        g.last_api_error = ""
        return True, info
    except Exception as e:
        xlog.exception("order e:%r", e)
        g.last_api_error = "%r" % e
        return False, "except:%r" % e


center_login_process = False


def request_balance(account=None, password=None, is_register=False, update_server=True, promoter=""):
    global center_login_process
    if not g.config.api_server:
        g.server_host = str("%s:%d" % (g.config.server_host, g.config.server_port))
        xlog.info("not api_server set, use server:%s specify in config.", g.server_host)
        return True, "success"

    if is_register:
        login_path = "/register"
        xlog.info("request_balance register:%s", account)
    else:
        login_path = "/login"

    if account is None:
        if not (g.config.login_account and g.config.login_password):
            xlog.debug("request_balance no account")
            return False, "no default account"

        account = g.config.login_account
        password = g.config.login_password

    req_info = {"account": account, "password": password, "protocol_version": "2",
                "promoter": promoter}

    try:
        center_login_process = True
        if g.tls_relay_front:
            g.tls_relay_front.set_x_tunnel_account(account, password)

        res, info = call_api(login_path, req_info)
        if not res:
            return False, info

        g.quota_list = info["quota_list"]
        g.quota = calculate_quota_left(g.quota_list)
        g.paypal_button_id = info["paypal_button_id"]
        g.plans = info["plans"]
        if g.quota <= 0:
            xlog.warn("no quota")

        if g.config.server_host:
            xlog.info("use server:%s specify in config.", g.config.server_host)
            g.server_host = str(g.config.server_host)
        elif update_server or not g.server_host:
            g.server_host = str(info["host"])
            g.server_port = info["port"]
            xlog.info("update xt_server %s:%d", g.server_host, g.server_port)

        g.selectable = info["selectable"]

        g.promote_code = info["promote_code"]
        g.promoter = info["promoter"]
        g.balance = info["balance"]
        xlog.info("request_balance host:%s port:%d balance:%f quota:%f", g.server_host, g.server_port,
                  g.balance, g.quota)
        return True, "success"
    except Exception as e:
        g.last_api_error = "login center except: %r" % e
        xlog.exception("request_balance e:%r", e)
        return False, e
    finally:
        center_login_process = False


login_lock = threading.Lock()


def login_process():
    with login_lock:
        if not (g.config.login_account and g.config.login_password):
            xlog.debug("x-tunnel no account")
            return False

        if not g.server_host:
            xlog.debug("session not running, try login..")
            res, reason = request_balance(g.config.login_account, g.config.login_password)
            if not res:
                xlog.warn("x-tunnel request_balance fail when create_conn:%s", reason)
                return False

        if time.time() - g.session.last_send_time > 5 * 60 - 5:
            xlog.info("session timeout, reset it.")
            g.session.stop()

        if not g.session.running:
            return g.session.start()

    return True


def create_conn(sock, host, port):
    if not (g.config.login_account and g.config.login_password):
        return False

    for _ in range(0, 3):
        if login_process():
            break
        else:
            time.sleep(1)

    return g.session.create_conn(sock, host, port)


def update_quota_loop():
    xlog.debug("update_quota_loop start.")

    start_time = time.time()
    last_quota = g.quota
    while g.running and time.time() - start_time < 10 * 60:
        if not g.config.login_account:
            xlog.info("update_quota_loop but logout.")
            return

        request_balance(
            g.config.login_account, g.config.login_password,
            is_register=False, update_server=False)

        if g.quota - last_quota > 1024 * 1024 * 1024:
            xlog.info("update_quota_loop quota updated")
            return

        time.sleep(60)

    xlog.warn("update_quota_loop timeout fail.")
