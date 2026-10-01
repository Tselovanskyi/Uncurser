"""SSH-only file access; all scripts are one-shot commands, never installed."""

import base64
import hashlib
import json
import shlex
from dataclasses import dataclass
from pathlib import Path

import paramiko


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
    keys = paramiko.HostKeys()
    if path.exists():
        keys.load(str(path))
    keys.add(error.hostname, error.key.get_name(), error.key)
    keys.save(str(path))


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
APPLY = '''import os,sys,json,base64,hashlib,stat,uuid,ast
r=json.load(sys.stdin); p=r["path"]; b=os.path.splitext(p)[0]+".ptn.Original.txt"; m=b+".json"
def sha(d): return hashlib.sha256(d).hexdigest()
def read(p):
 with open(p,"rb") as f: return f.read()
def regular(p):
 s=os.lstat(p)
 if not stat.S_ISREG(s.st_mode): raise ValueError("Not a regular file: "+p)
 return s
def syncdir():
 fd=os.open(os.path.dirname(p),os.O_RDONLY)
 try: os.fsync(fd)
 finally: os.close(fd)
def create(path,data,mode):
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,mode)
 with os.fdopen(fd,"wb") as f:
  f.write(data); f.flush(); os.fsync(f.fileno())
regular(p); old=read(p); s=os.stat(p)
if sha(old)!=r["expected"]: raise ValueError("File changed since preparation. Scan again; your staged edits are retained.")
new=base64.b64decode(r["data"],validate=True)
if sha(new)!=r["new_hash"]: raise ValueError("Upload checksum mismatch")
ast.parse(new.decode("utf-8"),filename=p)
if os.path.exists(b):
 regular(b); regular(m); meta=json.loads(read(m))
 if meta.get("path")!=p or meta.get("sha256")!=sha(read(b)): raise ValueError("Original backup or its manifest is damaged")
 if meta["sha256"]!=r["original_hash"]: raise ValueError("Original backup changed since scan")
elif r.get("original_hash") is not None:
 raise ValueError("Original backup disappeared since scan")
else:
 if not r["may_capture_original"]: raise ValueError("Cannot label an already modified file as the initial original")
 if os.path.exists(m): raise ValueError("Orphan original manifest requires review")
 create(b,old,0o444)
 meta=dict(path=p,sha256=sha(old),mode=stat.S_IMODE(s.st_mode),uid=s.st_uid,gid=s.st_gid,reference=r["reference"],firmware=r.get("firmware"),model=r.get("model"))
 create(m,json.dumps(meta,indent=2).encode(),0o444); syncdir()
tmp=p+".uncursed-pending-"+uuid.uuid4().hex+".txt"
try:
 create(tmp,new,stat.S_IMODE(s.st_mode))
 attrs=meta if r.get("restore") else dict(uid=s.st_uid,gid=s.st_gid,mode=stat.S_IMODE(s.st_mode))
 os.chown(tmp,attrs["uid"],attrs["gid"]); os.chmod(tmp,attrs["mode"])
 with open(tmp,"rb") as f: os.fsync(f.fileno())
 if sha(read(tmp))!=r["new_hash"]: raise ValueError("Prepared file checksum mismatch")
 regular(p)
 if sha(read(p))!=r["expected"]: raise ValueError("Working file changed during upload. Nothing replaced.")
 os.replace(tmp,p); syncdir()
 if sha(read(p))!=r["new_hash"]: raise ValueError("Working file changed after replacement; rescan before proceeding")
 print(json.dumps(dict(sha256=sha(new),original=meta["sha256"])))
finally:
 if os.path.exists(tmp): os.unlink(tmp)
'''


# Multiple staged mods share preflight, backup and candidate preparation. Renames
# are atomic per file; ordinary failures roll back only files still matching us.
APPLY_BATCH = '''import os,sys,json,base64,hashlib,stat,uuid,ast,glob,fnmatch,configparser
r=json.load(sys.stdin); items=r["files"]; prepared=[]; replaced=[]
def sha(data): return hashlib.sha256(data).hexdigest()
def read(path):
 s=os.lstat(path)
 if not stat.S_ISREG(s.st_mode): raise ValueError("Not a regular file: "+path)
 with open(path,"rb") as f: return f.read()
def syncdir(path):
 fd=os.open(os.path.dirname(path),os.O_RDONLY)
 try: os.fsync(fd)
 finally: os.close(fd)
def create(path,data,attrs):
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,attrs["mode"])
 with os.fdopen(fd,"wb") as f:
  f.write(data); os.fchown(f.fileno(),attrs["uid"],attrs["gid"]); os.fchmod(f.fileno(),attrs["mode"]); f.flush(); os.fsync(f.fileno())
def guards():
 for path,expected in r.get("guards",{}).items():
  if sha(read(path))!=expected: raise ValueError("Included configuration changed: "+path)
 for pattern,expected in r.get("patterns",{}).items():
  if sorted(glob.glob(pattern))!=expected: raise ValueError("Included configuration list changed: "+pattern)
def unchanged():
 guards()
 for item in prepared:
  if sha(read(item["path"]))!=item["expected"]: raise ValueError("Working file changed during preparation; scan again: "+item["path"])
try:
 for item in items:
  p=item["path"]; old=read(p); s=os.stat(p)
  if sha(old)!=item["expected"]: raise ValueError("Working file changed since scan: "+p)
  new=base64.b64decode(item["data"],validate=True)
  if sha(new)!=item["new_hash"]: raise ValueError("Upload checksum mismatch")
  if item["kind"]=="python": ast.parse(new.decode("utf-8"),filename=p)
  elif item["kind"]=="config":
   marker=b"#*# <---------------------- SAVE_CONFIG ---------------------->"
   if old.partition(marker)[1:]!=new.partition(marker)[1:]: raise ValueError("Generated calibration data would change")
   config=configparser.RawConfigParser(interpolation=None,strict=True,inline_comment_prefixes=("#",";"))
   config.read_string(new.partition(marker)[0].decode("utf-8"))
   for section,key,value in item["settings"]:
    if config.get(section,key,fallback=None)!=value: raise ValueError("Prepared configuration value mismatch: "+key)
  else: raise ValueError("Unsupported file type")
  attrs=dict(mode=stat.S_IMODE(s.st_mode),uid=s.st_uid,gid=s.st_gid)
  b=item["backup"]; m=b+".json"; meta=None
  if os.path.lexists(b) or os.path.lexists(m):
   saved=read(b); meta=json.loads(read(m))
   if meta.get("path")!=p or meta.get("sha256")!=sha(saved): raise ValueError("Original backup is damaged: "+b)
   if meta["sha256"]!=item["original_hash"]: raise ValueError("Original backup changed since scan")
  elif item["original_hash"] is not None: raise ValueError("Original backup disappeared")
  elif not item["may_capture_original"]: raise ValueError("An initial original is required for this mod")
  item.update(old=old,new=new,attrs=attrs,meta=meta,tmp=p+".uncursed-pending-"+uuid.uuid4().hex+".txt")
  for pattern in r.get("patterns",{}):
   if any(fnmatch.fnmatchcase(path,pattern) for path in (b,m,item["tmp"])): raise ValueError("A backup or temporary file would match a config include; review that include first")
  prepared.append(item)
 unchanged()
 # Every required original is secured before any working file is replaced.
 for item in prepared:
  if item["meta"] is None:
   meta=dict(item["attrs"],path=item["path"],sha256=sha(item["old"]),reference=item["reference"],firmware=r.get("firmware"),model=r.get("model"))
   attrs=dict(item["attrs"],mode=0o444)
   create(item["backup"],item["old"],attrs)
   create(item["backup"]+".json",json.dumps(meta,indent=2).encode(),attrs)
   syncdir(item["backup"]); item["meta"]=meta
  attrs=item["meta"] if item.get("restore") else item["attrs"]
  create(item["tmp"],item["new"],attrs)
  if sha(read(item["tmp"]))!=item["new_hash"]: raise ValueError("Prepared file checksum mismatch")
 unchanged()
 for item in prepared:
  guards()
  if sha(read(item["path"]))!=item["expected"]: raise ValueError("Working file changed before replacement")
  os.replace(item["tmp"],item["path"]); replaced.append(item); syncdir(item["path"])
  if sha(read(item["path"]))!=item["new_hash"]: raise ValueError("Working file changed after replacement")
 print(json.dumps(dict(saved=[item["path"] for item in replaced])))
except Exception as error:
 failures=[]
 for item in reversed(replaced):
  try:
   if sha(read(item["path"]))!=item["new_hash"]: raise ValueError("changed by another writer")
   create(item["tmp"],item["old"],item["attrs"])
   if sha(read(item["path"]))!=item["new_hash"]: raise ValueError("changed during rollback")
   os.replace(item["tmp"],item["path"]); syncdir(item["path"])
  except Exception: failures.append(item["path"])
 if failures: raise ValueError("Apply was interrupted; some changes may remain. Rescan: "+", ".join(failures)) from error
 raise
finally:
 for item in prepared:
  if os.path.exists(item["tmp"]): os.unlink(item["tmp"])
'''


class Remote:
    def __init__(self, connection: Connection, known_hosts: Path):
        self.connection = connection
        self.client = paramiko.SSHClient()
        if known_hosts.exists():
            self.client.load_host_keys(str(known_hosts))
        self.client.set_missing_host_key_policy(AskHostKey())

    def __enter__(self):
        c = self.connection
        try:
            self.client.connect(c.host, port=c.port, username=c.username, password=c.password,
                                allow_agent=False, look_for_keys=False, timeout=6, auth_timeout=8, banner_timeout=6)
        except Exception:
            self.client.close()
            raise
        return self

    def __exit__(self, *args):
        self.client.close()

    def python(self, program: str, payload=None, timeout=30):
        stdin, stdout, stderr = self.client.exec_command("python3 -c " + shlex.quote(program), timeout=timeout)
        stdin.write(json.dumps(payload or {})); stdin.flush(); stdin.channel.shutdown_write()
        output = stdout.read(8 * 1024 * 1024)
        error = stderr.read(128 * 1024)
        code = stdout.channel.recv_exit_status()
        if code:
            message = error.decode("utf-8", errors="replace").strip().splitlines()
            raise RuntimeError(message[-1] if message else f"Printer command failed ({code})")
        return json.loads(output)

    def read(self, path):
        result = self.python(READ, {"path": path})
        if result is not None:
            result["data"] = base64.b64decode(result["data"])
        return result

    def identity(self):
        return self.python('''import platform,os,json
print(json.dumps(dict(hostname=platform.node(),machine=platform.machine(),python=platform.python_version(),history=os.path.isfile("/mnt/UDISK/creality/userdata/history/print_history_record.json"))))
''')
