"""SSH-only file access; all scripts are one-shot commands, never installed."""

import base64
import hashlib
import json
import shlex
import threading
from dataclasses import dataclass
from pathlib import Path

import paramiko

from . import settings


@dataclass
class Connection:
    host: str
    port: int = 22
    username: str = "root"
    password: str = ""


class NewHostKey(Exception):
    def __init__(self, hostname, key):
        self.hostname, self.key = hostname, key
        self.fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
        super().__init__(f"{hostname}\n{key.get_name()}\n{self.fingerprint}")


class AskHostKey(paramiko.MissingHostKeyPolicy):
    def missing_host_key(self, client, hostname, key):
        raise NewHostKey(hostname, key)


def trust_key(path: Path, error: NewHostKey):
    keys = settings.host_keys(settings.load(path))
    keys.add(error.hostname, error.key.get_name(), error.key)
    settings.update(path, ssh_host_keys=settings.encode_keys(keys))


READ = '''import os,sys,json,base64,hashlib,stat
p=json.load(sys.stdin)["path"]
try:
 s=os.lstat(p)
 if not stat.S_ISREG(s.st_mode): raise ValueError("Expected a regular file: "+p)
 if s.st_size>2097152: raise ValueError("File exceeds supported size")
 with open(p,"rb") as f: d=f.read()
 print(json.dumps(dict(data=base64.b64encode(d).decode(),sha256=hashlib.sha256(d).hexdigest(),mode=stat.S_IMODE(s.st_mode),uid=s.st_uid,gid=s.st_gid)))
except FileNotFoundError: print("null")
'''

READ_MANY = '''import os,sys,json,base64,hashlib,stat
paths=json.load(sys.stdin)["paths"]; result={}
for p in dict.fromkeys(paths):
 try:
  s=os.lstat(p)
  if not stat.S_ISREG(s.st_mode): raise ValueError("Expected a regular file: "+p)
  if s.st_size>2097152: raise ValueError("File exceeds supported size")
  with open(p,"rb") as f: d=f.read(2097153)
  if len(d)>2097152: raise ValueError("File exceeds supported size")
  result[p]=dict(data=base64.b64encode(d).decode(),sha256=hashlib.sha256(d).hexdigest(),mode=stat.S_IMODE(s.st_mode),uid=s.st_uid,gid=s.st_gid)
 except FileNotFoundError: result[p]=None
 except Exception as error: result[p]=dict(error=type(error).__name__+": "+str(error))
print(json.dumps(result))
'''

# Reads the active config and every include; never executes G-code or writes files.
CONFIG_TREE = '''import os,sys,json,base64,hashlib,stat,re,glob
root=json.load(sys.stdin)["path"]; folder=os.path.dirname(root)
files={}; patterns={}
def visit(path):
 if path in files: raise ValueError("Repeated or circular config include: "+path)
 if len(files)>=64: raise ValueError("Too many included config files")
 if os.path.commonpath([folder,os.path.realpath(path)])!=folder: raise ValueError("Config include is outside the config directory")
 s=os.lstat(path)
 if not stat.S_ISREG(s.st_mode) or s.st_size>2097152: raise ValueError("Unsupported config file: "+path)
 with open(path,"rb") as f: data=f.read()
 files[path]=dict(data=base64.b64encode(data).decode(),sha256=hashlib.sha256(data).hexdigest())
 for line in data.decode("utf-8").splitlines():
  match=re.fullmatch(r"\\s*\\[include ([^\\]]+)\\]\\s*(?:[#;].*)?",line)
  if not match: continue
  pattern=os.path.normpath(os.path.join(os.path.dirname(path),match.group(1).strip()))
  matches=sorted(glob.glob(pattern))
  if not matches: raise ValueError("Config include has no matching files: "+pattern)
  patterns[pattern]=matches
  for child in matches: visit(child)
visit(root)
print(json.dumps(dict(files=files,patterns=patterns)))
'''

# No historical snapshots: one immutable original and an adjacent candidate.
# O_EXCL creation prevents a second app from overwriting the original baseline.
# Multiple staged mods share preflight, backup and candidate preparation. Renames
# are atomic per file; ordinary failures roll back only files still matching us.
APPLY_BATCH = '''import os,sys,json,base64,hashlib,stat,uuid,ast,glob,fnmatch,configparser,re
r=json.load(sys.stdin); items=r["files"]; prepared=[]; replaced=[]; caches=[]; cleared=[]
def sha(data): return hashlib.sha256(data).hexdigest()
def read(path):
 try: s=os.lstat(path)
 except FileNotFoundError: return None
 if not stat.S_ISREG(s.st_mode): raise ValueError("Not a regular file: "+path)
 with open(path,"rb") as f: return f.read()
def fingerprint(data): return sha(data) if data is not None else None
def syncdir(path):
 fd=os.open(os.path.dirname(path),os.O_RDONLY)
 try: os.fsync(fd)
 finally: os.close(fd)
def create(path,data,attrs):
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,attrs["mode"])
 with os.fdopen(fd,"wb") as f:
  f.write(data); os.fchown(f.fileno(),attrs["uid"],attrs["gid"]); os.fchmod(f.fileno(),attrs["mode"]); f.flush(); os.fsync(f.fileno())
def cache_paths(source):
 folder=os.path.dirname(source); stem=os.path.basename(source)[:-3]
 paths=[source+"c",source+"o"]; cache_folder=os.path.join(folder,"__pycache__")
 if os.path.lexists(cache_folder):
  if not stat.S_ISDIR(os.lstat(cache_folder).st_mode): raise ValueError("Not a regular cache directory: "+cache_folder)
  pattern=re.compile(re.escape(stem)+r"\\.[A-Za-z0-9_-]+(?:\\.opt-[0-9]+)?\\.pyc")
  paths.extend(os.path.join(cache_folder,name) for name in sorted(os.listdir(cache_folder)) if pattern.fullmatch(name))
 return paths
def capture_caches(source):
 for path in cache_paths(source):
  if not os.path.lexists(path): continue
  attrs=os.lstat(path)
  if not stat.S_ISREG(attrs.st_mode) or attrs.st_size>2097152: raise ValueError("Unsupported Python cache: "+path)
  data=read(path)
  if data is None or len(data)>2097152: raise ValueError("Python cache changed during preparation: "+path)
  caches.append(dict(path=path,old=data,expected=sha(data),attrs=dict(mode=stat.S_IMODE(attrs.st_mode),uid=attrs.st_uid,gid=attrs.st_gid),tmp=path+".uncursed-pending-"+uuid.uuid4().hex+".txt"))
def guards():
 for path,expected in r.get("guards",{}).items():
  if fingerprint(read(path))!=expected: raise ValueError("Included configuration changed: "+path)
 for pattern,expected in r.get("patterns",{}).items():
  expected=set(expected)
  for item in replaced:
   if fnmatch.fnmatchcase(item["path"],pattern):
    if item["new"] is None: expected.discard(item["path"])
    else: expected.add(item["path"])
  if sorted(glob.glob(pattern))!=sorted(expected): raise ValueError("Included configuration list changed: "+pattern)
def unchanged():
 guards()
 for item in prepared:
  if fingerprint(read(item["path"]))!=item["expected"]: raise ValueError("Working file changed during preparation; scan again: "+item["path"])
 for path,expected in cache_sources.items():
  if fingerprint(read(path))!=expected: raise ValueError("Python source changed before cache cleanup: "+path)
 for item in caches:
  if fingerprint(read(item["path"]))!=item["expected"]: raise ValueError("Python cache changed during preparation: "+item["path"])
try:
 for item in items:
  p=item["path"]; old=read(p); s=os.stat(p if old is not None else os.path.dirname(p))
  if fingerprint(old)!=item["expected"]: raise ValueError("Working file changed since scan: "+p)
  new=base64.b64decode(item["data"],validate=True) if item["data"] is not None else None
  if fingerprint(new)!=item["new_hash"]: raise ValueError("Upload checksum mismatch")
  if new is None:
   if not item.get("original_absent"): raise ValueError("Only files originally absent may be removed")
  elif item["kind"]=="python": ast.parse(new.decode("utf-8"),filename=p)
  elif item["kind"]=="config":
   marker=b"#*# <---------------------- SAVE_CONFIG ---------------------->"
   if (old or b"").partition(marker)[1:]!=new.partition(marker)[1:]: raise ValueError("Generated calibration data would change")
   config=configparser.RawConfigParser(interpolation=None,strict=True,inline_comment_prefixes=("#",";"))
   config.read_string(new.partition(marker)[0].decode("utf-8"))
   for section,key,value in item.get("settings",[]):
    if config.get(section,key,fallback=None)!=value: raise ValueError("Prepared configuration value mismatch: "+key)
  elif item["kind"]=="javascript":
   if old is None: raise ValueError("The existing Fluidd file is missing")
   if item.get("restore"):
    if new!=read(item["backup"]): raise ValueError("Fluidd restoration does not match its original backup")
   else:
    text=old.decode("utf-8")
    for before,after in item["replacements"]:
     if not before or text.count(before)!=1: raise ValueError("Fluidd edit is not unique")
     text=text.replace(before,after,1)
    if text.encode("utf-8")!=new: raise ValueError("Unrelated Fluidd code would change")
  elif item["kind"]=="json":
   document=json.loads(new.decode("utf-8"))
   if not isinstance(document,dict) or document.get("schema")!=1 or not isinstance(document.get("mods"),dict): raise ValueError("Invalid mod records")
  else: raise ValueError("Unsupported file type")
  attrs=dict(mode=stat.S_IMODE(s.st_mode) if old is not None else 0o644,uid=s.st_uid,gid=s.st_gid)
  b=item["backup"]; m=b+".json"; meta=None
  if os.path.lexists(b) or os.path.lexists(m):
   saved=read(b); meta=json.loads(read(m))
   if saved is None or meta.get("path")!=p or meta.get("sha256")!=sha(saved): raise ValueError("Original backup is damaged: "+b)
   if meta.get("absent",False)!=item.get("original_absent",False) or (meta.get("absent") and saved!=b""): raise ValueError("Original absence record is damaged")
   if meta["sha256"]!=item["original_hash"]: raise ValueError("Original backup changed since scan")
  elif item["original_hash"] is not None: raise ValueError("Original backup disappeared")
  elif not item["may_capture_original"]: raise ValueError("An initial original is required for this mod")
  item.update(old=old,new=new,attrs=attrs,meta=meta,tmp=p+".uncursed-pending-"+uuid.uuid4().hex+".txt")
  for pattern in r.get("patterns",{}):
   if old is None and item["kind"]!="config" and fnmatch.fnmatchcase(p,pattern): raise ValueError("A new non-config file would match a config include; review that include first")
   if any(fnmatch.fnmatchcase(path,pattern) for path in (b,m,item["tmp"])): raise ValueError("A backup or temporary file would match a config include; review that include first")
  prepared.append(item)
 # Generated bytecode is disposable, not part of immutable mod originals.
 cache_sources=dict(r.get("python_caches",{}))
 for item in prepared:
  if item["kind"]=="python":
   if item["path"] in cache_sources and cache_sources[item["path"]]!=item["expected"]: raise ValueError("Python cache source guard disagrees with its file operation")
   cache_sources[item["path"]]=item["expected"]
 for source in cache_sources:
  if not source.endswith(".py"): raise ValueError("Cache cleanup requires a Python source path")
  capture_caches(source)
 unchanged()
 # Every required original is secured before any working file is replaced.
 for item in prepared:
  if item["meta"] is None:
   meta=dict(item["attrs"],path=item["path"],sha256=sha(item["old"] or b""),absent=item["old"] is None,reference=item["reference"],firmware=r.get("firmware"),model=r.get("model"))
   attrs=dict(item["attrs"],mode=0o444)
   create(item["backup"],item["old"] or b"",attrs)
   create(item["backup"]+".json",json.dumps(meta,indent=2).encode(),attrs)
   syncdir(item["backup"]); item["meta"]=meta
  attrs=item["meta"] if item.get("restore") else item["attrs"]
  if item["new"] is not None:
   create(item["tmp"],item["new"],attrs)
   if sha(read(item["tmp"]))!=item["new_hash"]: raise ValueError("Prepared file checksum mismatch")
 unchanged()
 # Capture caches only for rollback; never create permanent cache backups.
 for item in caches:
  guards()
  if fingerprint(read(item["path"]))!=item["expected"]: raise ValueError("Python cache changed before cleanup: "+item["path"])
  os.unlink(item["path"]); cleared.append(item); syncdir(item["path"])
 for item in prepared:
  guards()
  if fingerprint(read(item["path"]))!=item["expected"]: raise ValueError("Working file changed before replacement")
  if item["new"] is None: os.unlink(item["path"])
  elif item["old"] is None: os.link(item["tmp"],item["path"])
  else: os.replace(item["tmp"],item["path"])
  replaced.append(item); syncdir(item["path"])
  if os.path.exists(item["tmp"]): os.unlink(item["tmp"])
  if fingerprint(read(item["path"]))!=item["new_hash"]: raise ValueError("Working file changed after replacement")
 for source in cache_sources:
  for path in cache_paths(source):
   if os.path.lexists(path): raise ValueError("Python cache was recreated during Apply; rescan: "+path)
 print(json.dumps(dict(saved=[item["path"] for item in replaced],cleared=[item["path"] for item in cleared])))
except Exception as error:
 failures=[]
 for item in reversed(replaced):
  try:
   if fingerprint(read(item["path"]))!=item["new_hash"]: raise ValueError("changed by another writer")
   if os.path.exists(item["tmp"]): os.unlink(item["tmp"])
   if item["old"] is None: os.unlink(item["path"])
   else:
    create(item["tmp"],item["old"],item["attrs"])
    if fingerprint(read(item["path"]))!=item["new_hash"]: raise ValueError("changed during rollback")
    if item["new"] is None: os.link(item["tmp"],item["path"])
    else: os.replace(item["tmp"],item["path"])
   syncdir(item["path"])
  except Exception: failures.append(item["path"])
 for item in reversed(cleared):
  try:
   if read(item["path"]) is not None: raise ValueError("cache recreated by another writer")
   create(item["tmp"],item["old"],item["attrs"])
   os.link(item["tmp"],item["path"]); syncdir(item["path"])
  except Exception: failures.append(item["path"])
 if failures: raise ValueError("Apply was interrupted; some changes may remain. Rescan: "+", ".join(failures)) from error
 raise
finally:
 for item in prepared+caches:
  if os.path.exists(item["tmp"]): os.unlink(item["tmp"])
'''


def _ssh_error_message(error, operation):
    return ("The printer's SSH connection timed out while " if isinstance(error, TimeoutError)
            else "The printer closed the SSH connection while ") + operation + "."


# Keep SSH's command request short. Source is decoded and executed in memory;
# the program reads its unchanged JSON request from the rest of stdin.
PYTHON_BOOTSTRAP = 'import sys,json;exec(compile(json.loads(sys.stdin.readline()),"<uncurser>","exec"))'


class Remote:
    def __init__(self, connection: Connection, known_hosts: Path):
        self.connection = connection
        self._read_cache = {}
        self.client = paramiko.SSHClient()
        for hostname, records in settings.host_keys(settings.load(known_hosts)).items():
            for kind, key in records.items():
                self.client.get_host_keys().add(hostname, kind, key)
        self.client.set_missing_host_key_policy(AskHostKey())

    def __enter__(self):
        self._read_cache = {}
        c = self.connection
        try:
            self.client.connect(c.host, port=c.port, username=c.username, password=c.password,
                                allow_agent=False, look_for_keys=False, timeout=6, auth_timeout=8, banner_timeout=6)
        except Exception as error:
            self.client.close()
            if isinstance(error, (TimeoutError, EOFError)) and not str(error).strip():
                raise RuntimeError(_ssh_error_message(error, "connecting")) from error
            raise
        return self

    def __exit__(self, *args):
        self._read_cache = {}
        self.client.close()

    def python(self, program: str, payload=None, timeout=30, *, max_output_bytes=8 * 1024 * 1024):
        if not isinstance(max_output_bytes, int) or max_output_bytes <= 0:
            raise ValueError("The printer output limit must be a positive byte count.")
        try:
            stdin, stdout, stderr = self.client.exec_command("python3 -B -c " + shlex.quote(PYTHON_BOOTSTRAP), timeout=timeout)
        except (TimeoutError, EOFError) as error:
            if str(error).strip():
                raise
            raise RuntimeError(_ssh_error_message(error, "opening a command session")) from error
        try:
            stdin.write(json.dumps(program) + "\n" + json.dumps(payload or {})); stdin.flush(); stdin.channel.shutdown_write()
        except Exception as error:
            stdout.channel.close()
            if isinstance(error, (TimeoutError, EOFError)) and not str(error).strip():
                raise RuntimeError(_ssh_error_message(error, "sending command data")) from error
            raise

        def bounded(stream, limit):
            chunks, size = [], 0
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    return b"".join(chunks)
                size += len(chunk)
                if size > limit:
                    raise RuntimeError("Printer command output exceeds the supported size.")
                chunks.append(chunk)

        error_result, error_done = {}, threading.Event()
        def read_error():
            try:
                error_result["data"] = bounded(stderr, 128 * 1024)
            except Exception as exc:
                error_result["error"] = exc
                stdout.channel.close()
            finally:
                error_done.set()
        reader = threading.Thread(target=read_error, daemon=True)
        reader.start()
        try:
            output = bounded(stdout, max_output_bytes)
            if not error_done.wait(timeout):
                raise RuntimeError("Timed out reading the printer command response.")
            if "error" in error_result:
                raise error_result["error"]
            error = error_result["data"]
            code = stdout.channel.recv_exit_status()
        except (TimeoutError, EOFError) as error:
            if str(error).strip():
                raise
            raise RuntimeError(_ssh_error_message(error, "reading the command response")) from error
        finally:
            stdout.channel.close()
            reader.join(timeout=0.1)
        if code:
            message = error.decode("utf-8", errors="replace").strip().splitlines()
            raise RuntimeError(message[-1] if message else f"Printer command failed ({code})")
        return json.loads(output)

    def prefetch(self, paths):
        self._read_cache = {}
        paths = list(dict.fromkeys(paths))
        if not paths:
            return
        # Every file is limited to 2 MiB. Include base64 expansion, serialized
        # path/error text and per-record attributes in the response bound.
        limit = len(paths) * (4 * ((2097152 + 2) // 3) + 4096) + 2 * len(json.dumps(paths).encode()) + 4096
        result = self.python(READ_MANY, {"paths": paths}, max_output_bytes=limit)
        if not isinstance(result, dict) or set(result) != set(paths):
            raise RuntimeError("The printer returned an incomplete file scan.")
        cached = {}
        for path, value in result.items():
            if value is not None:
                value = dict(value)
                if "error" not in value:
                    value["data"] = base64.b64decode(value["data"], validate=True)
            cached[path] = value
        self._read_cache = cached

    def read(self, path):
        if path in self._read_cache:
            result = self._read_cache[path]
            if result is not None and "error" in result:
                raise RuntimeError(result["error"])
            return dict(result) if result is not None else None
        result = self.python(READ, {"path": path})
        if result is not None:
            result["data"] = base64.b64decode(result["data"])
        return result

    def identity(self):
        return self.python('''import platform,os,json
print(json.dumps(dict(hostname=platform.node(),machine=platform.machine(),python=platform.python_version(),history=os.path.isfile("/mnt/UDISK/creality/userdata/history/print_history_record.json"))))
''')
