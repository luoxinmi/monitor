#!/usr/bin/env python3
"""
白鲸节点 vmess 兼容中继
-----------------------
白鲸服务端校验 vmess 请求头 version 字节 == 101（标准 v2ray/xray 发 1），
并且 fnv1a 校验覆盖 version 字节，所以必须「解密 → 改 version → 重算 fnv → 重封装」。

用法:
    python3 whale_vmess_relay.py --listen 127.0.0.1:19233 --target 82.38.80.138:9233
"""
import sys, os, socket, struct, zlib, hashlib, time, argparse, threading, logging

from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes

UUID_DEFAULT = "be58f4e4-894a-47ee-8836-cf75e74fa9c9"
VMESS_VERSION = 101          # 白鲸要求的 magic


# ---------- 标准 vmess AEAD 密码学 ----------
def _pure_sha256(b): return hashlib.sha256(b).digest()

def _hmac_pure(key, base):
    bs = 64
    k = key if len(key) <= bs else base(key)
    k = k.ljust(bs, b"\x00")
    ipad = bytes(x ^ 0x36 for x in k); opad = bytes(x ^ 0x5c for x in k)
    return lambda d: base(opad + base(ipad + d))

def kdf(key, *paths):
    h = _pure_sha256
    for p in (b"VMess AEAD KDF",) + tuple(paths):
        h = _hmac_pure(p, h)
    return h(key)

def kdf16(key, *paths): return kdf(key, *paths)[:16]


class VmessCodec:
    def __init__(self, uuid=UUID_DEFAULT):
        self.cmdkey = hashlib.md5(
            bytes.fromhex(uuid.replace("-", "")) + b"c48619fe-8f02-49e0-b9e9-edf763e17e21"
        ).digest()
        self.idkey = kdf16(self.cmdkey, b"AES Auth ID Encryption")

    def create_authid(self, ts=None):
        ts = int(time.time()) if ts is None else ts
        buf = struct.pack(">q", ts) + get_random_bytes(4)
        buf += struct.pack(">I", zlib.crc32(buf) & 0xffffffff)
        return AES.new(self.idkey, AES.MODE_ECB).encrypt(buf)

    def open_header(self, payload):
        """解出 (plain_header, header_total_len) 或 (None, None)"""
        if len(payload) < 58:
            return None, None
        aid = payload[0:16]; nonce = payload[34:42]
        lk = kdf16(self.cmdkey, b"VMess Header AEAD Key_Length", aid, nonce)
        ln = kdf(self.cmdkey, b"VMess Header AEAD Nonce_Length", aid, nonce)[:12]
        try:
            c = AES.new(lk, AES.MODE_GCM, nonce=ln); c.update(aid)
            lraw = c.decrypt_and_verify(payload[16:18], payload[18:34])
            hlen = struct.unpack(">H", lraw)[0]
            pk = kdf16(self.cmdkey, b"VMess Header AEAD Key", aid, nonce)
            pn = kdf(self.cmdkey, b"VMess Header AEAD Nonce", aid, nonce)[:12]
            c2 = AES.new(pk, AES.MODE_GCM, nonce=pn); c2.update(aid)
            end = 42 + hlen + 16
            if len(payload) < end:
                return None, None
            plain = c2.decrypt_and_verify(payload[42:42 + hlen], payload[42 + hlen:end])
            return plain, end
        except Exception:
            return None, None

    def seal_header(self, plain):
        aid = self.create_authid()
        nonce = get_random_bytes(8)
        lk = kdf16(self.cmdkey, b"VMess Header AEAD Key_Length", aid, nonce)
        ln = kdf(self.cmdkey, b"VMess Header AEAD Nonce_Length", aid, nonce)[:12]
        c = AES.new(lk, AES.MODE_GCM, nonce=ln); c.update(aid)
        ld, lt = c.encrypt_and_digest(struct.pack(">H", len(plain)))
        pk = kdf16(self.cmdkey, b"VMess Header AEAD Key", aid, nonce)
        pn = kdf(self.cmdkey, b"VMess Header AEAD Nonce", aid, nonce)[:12]
        c2 = AES.new(pk, AES.MODE_GCM, nonce=pn); c2.update(aid)
        pd, pt = c2.encrypt_and_digest(plain)
        return aid + ld + lt + nonce + pd + pt


def fix_version(plain):
    """把 version 改成 101 并重算 fnv1a"""
    if not plain or len(plain) < 5:
        return plain
    b = bytearray(plain)
    b[0] = VMESS_VERSION
    h = 0x811c9dc5
    for x in bytes(b[:-4]):
        h = (h ^ x) & 0xffffffff
        h = (h * 0x01000193) & 0xffffffff
    b[-4:] = struct.pack(">I", h)
    return bytes(b)


# ---------- websocket 帧 ----------
def ws_frame(payload):
    m = get_random_bytes(4); n = len(payload)
    if n < 126:
        hdr = bytes([0x82, 0x80 | n])
    elif n < 65536:
        hdr = bytes([0x82, 0x80 | 126]) + struct.pack(">H", n)
    else:
        hdr = bytes([0x82, 0x80 | 127]) + struct.pack(">Q", n)
    return hdr + m + bytes(c ^ m[i % 4] for i, c in enumerate(payload))


def ws_unframe(buf):
    out = []; pos = 0
    while pos + 2 <= len(buf):
        b0, b1 = buf[pos], buf[pos + 1]
        ln = b1 & 0x7F; p = pos + 2
        if ln == 126:
            if p + 2 > len(buf): break
            ln = struct.unpack(">H", buf[p:p + 2])[0]; p += 2
        elif ln == 127:
            if p + 8 > len(buf): break
            ln = struct.unpack(">Q", buf[p:p + 8])[0]; p += 8
        masked = b1 & 0x80
        if masked:
            if p + 4 > len(buf): break
            mask = buf[p:p + 4]; p += 4
        if p + ln > len(buf): break
        pl = buf[p:p + ln]
        if masked:
            pl = bytes(c ^ mask[i % 4] for i, c in enumerate(pl))
        out.append((b0, pl)); pos = p + ln
    return out, pos


class WhaleRelay:
    def __init__(self, listen, target, uuid=UUID_DEFAULT, log=None):
        self.listen = listen; self.target = target
        self.codec = VmessCodec(uuid)
        self.log = log or logging.getLogger("whale")

    def pump(self, src, dst, direction):
        try:
            if direction == "up":
                self._pump_up(src, dst)
            else:
                while True:
                    d = src.recv(65536)
                    if not d: break
                    dst.sendall(d)
        except Exception as e:
            self.log.debug("%s err %s", direction, e)
        finally:
            for s in (src, dst):
                try: s.shutdown(socket.SHUT_RDWR)
                except Exception: pass

    def _pump_up(self, src, dst):
        """上行：首帧做 version 手术，之后透传"""
        buf = b""; mutated = False
        while True:
            d = src.recv(65536)
            if not d: break
            if mutated:
                dst.sendall(d); continue
            if d.startswith(b"GET") or buf == b"" and d.startswith(b"GET"):
                buf += d
                if b"\r\n\r\n" in buf:
                    dst.sendall(buf); buf = b""
                    continue
                continue
            buf += d
            frames, consumed = ws_unframe(buf)
            if not frames:
                continue
            b0, pl = frames[0]
            if (b0 & 0x0F) != 0x2:
                dst.sendall(buf); buf = b""; mutated = True; continue
            plain, hlen = self.codec.open_header(pl)
            if plain is None:
                self.log.warning("首帧解码失败，原样透传")
                dst.sendall(buf); buf = b""; mutated = True; continue
            new_plain = fix_version(plain)
            tail = pl[hlen:]
            self.log.info("首帧 version %d -> %d (hlen=%d tail=%d)",
                          plain[0], new_plain[0], len(plain), len(tail))
            dst.sendall(ws_frame(self.codec.seal_header(new_plain) + tail))
            rest = buf[consumed:]
            if rest:
                dst.sendall(rest)
            buf = b""; mutated = True

    def handle(self, c):
        try:
            u = socket.create_connection(self.target, timeout=10)
        except Exception as e:
            self.log.error("连目标失败 %s: %s", self.target, e); c.close(); return
        t1 = threading.Thread(target=self.pump, args=(c, u, "up"), daemon=True)
        t2 = threading.Thread(target=self.pump, args=(u, c, "down"), daemon=True)
        t1.start(); t2.start()

    def serve(self):
        host, port = self.listen
        srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, port)); srv.listen(64)
        self.log.info("listening %s:%d -> %s:%d (version=%d)",
                      host, port, self.target[0], self.target[1], VMESS_VERSION)
        while True:
            c, _ = srv.accept()
            threading.Thread(target=self.handle, args=(c,), daemon=True).start()


def parse_addr(s):
    if ":" not in s: raise argparse.ArgumentTypeError(f"需要 host:port，收到 {s}")
    h, p = s.rsplit(":", 1)
    return (h, int(p))


def main():
    ap = argparse.ArgumentParser(description="白鲸 vmess version 兼容中继")
    ap.add_argument("--listen", type=parse_addr, default=("127.0.0.1", 19233))
    ap.add_argument("--target", type=parse_addr, default=("82.38.80.138", 9233))
    ap.add_argument("--uuid", default=UUID_DEFAULT)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    WhaleRelay(a.listen, a.target, a.uuid,
               logging.getLogger("whale")).serve()


if __name__ == "__main__":
    main()
