"""Length-framed local Unix RPC. Cooperative single-user surface; no Python deserialization."""
from __future__ import annotations
import os
import socket
import socketserver
import struct
import threading
from pathlib import Path
from .common import Fault,canonical,need,obj,parse_json,uid
MAX_FRAME=8*1024*1024

def receive(sock):
    def read(n):
        chunks=[];count=0
        while count<n:
            part=sock.recv(n-count)
            if not part:raise Fault('truncated_request','Connection ended before complete frame')
            chunks.append(part);count+=len(part)
        return b''.join(chunks)
    length=struct.unpack('!I',read(4))[0];need(0<length<=MAX_FRAME,'too_large','RPC frame limit exceeded')
    return parse_json(read(length))

def send(sock,value):
    data=canonical(value);need(len(data)<=MAX_FRAME,'too_large','Paginate the result instead of emitting a huge frame')
    sock.sendall(struct.pack('!I',len(data))+data)

def _rejection(value):
    """Decode only a complete, typed server rejection envelope."""
    if not isinstance(value,dict) or set(value)!={'ok','error'} or value.get('ok') is not False:
        return None
    error=value.get('error')
    if not isinstance(error,dict) or not set(error)<= {'code','message','details'} or not {'code','message'}<=set(error):
        return None
    code,message=error['code'],error['message']
    if not isinstance(code,str) or not code.strip() or not isinstance(message,str) or not message.strip():
        return None
    return Fault(code,message,error.get('details'))

class Server(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
    daemon_threads=True;block_on_close=True
    def __init__(self,control,path):
        self.control=control;self.path=Path(path).absolute();self.slots=threading.BoundedSemaphore(64)
        need(len(os.fsencode(self.path))<104,'socket_path_too_long','Choose a shorter local Unix socket path')
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o711)
        if self.path.exists():
            probe=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            try:probe.connect(str(self.path));raise Fault('already_running','Socket is in use')
            except (ConnectionRefusedError,FileNotFoundError):self.path.unlink()
            finally:probe.close()
        server=self
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(30)
                try:
                    envelope=receive(self.request);obj(envelope,required=('request',),optional=('token',))
                    result=server.control.request(envelope.get('token'),envelope['request'])
                    send(self.request,{'ok':True,'result':result})
                    if server.control.shutdown_requested:
                        threading.Thread(target=server.shutdown, daemon=True).start()
                except Fault as exc:send(self.request,{'ok':False,'error':exc.as_dict()})
                except (ConnectionError,socket.timeout):return
                except Exception as exc:
                    # Internal stack traces never go to workers.
                    try:server.control.sec.event(None,'rpc_internal_error','server',{'type':type(exc).__name__,'message':str(exc)[:1500]})
                    except Exception:pass
                    try:send(self.request,{'ok':False,'error':{'code':'internal_error','message':'Control request failed; consult the local audit log.'}})
                    except OSError:pass
        super().__init__(str(self.path),Handler)
        import sys
        control.rt.review_connection = {
            'command': [sys.executable, '-m', 'daikibo', '--socket', str(self.path)],
            'semantics': 'Local cooperative controller; use only read operations during review.'}
        # Local socket uses ordinary filesystem defaults. No token authentication.
    def process_request(self,request,client_address):
        # Acquire before spawning a thread. Waiting inside handle() allows unlimited threads.
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(0.1)
                send(request,{'ok':False,'error':{'code':'capacity','message':'Control request capacity exceeded; retry with bounded backoff.'}})
            except OSError:pass
            finally:self.shutdown_request(request)
            return
        try:super().process_request(request,client_address)
        except BaseException:
            self.slots.release();raise

    def process_request_thread(self,request,client_address):
        try:super().process_request_thread(request,client_address)
        finally:self.slots.release()

    def server_close(self):
        super().server_close()
        try:self.path.unlink()
        except FileNotFoundError:pass

class Client:
    def __init__(self,path,token=None,timeout=60):self.path,self.token,self.timeout=str(path),token,timeout
    def call(self,method,params=None,request_id=None):
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
            sock.settimeout(self.timeout)
            try:sock.connect(self.path)
            except OSError as exc:raise Fault('control_unavailable','Cannot connect to local controller',str(exc)) from exc
            request={'token':self.token,'request':{'id':request_id or uid('REQ'),'method':method,'params':params or {}}}
            try:
                send(sock,request)
            except ConnectionError as send_error:
                # A saturated Server may have sent its typed capacity rejection
                # and closed this connection while the request was still being
                # written.  Read that one already-buffered response only; never
                # retry or turn an ambiguous/success response into acceptance.
                try:
                    rejection=_rejection(receive(sock))
                except (Fault,OSError):
                    raise send_error
                if rejection is not None:raise rejection
                raise send_error
            result=receive(sock)
        if not result.get('ok'):
            e=result.get('error',{});raise Fault(e.get('code','rpc_error'),e.get('message','Request rejected'),e.get('details'))
        return result['result']
