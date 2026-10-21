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
 if os.path.commonpath([folder,os.path.realpath(path)])!=folder and not (path=="/usr/share/klipper/klippy/extras/screws_tilt_adjust.cfg" and os.path.realpath(path)==path): raise ValueError("Config include is outside the config directory")
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
APPLY_BATCH = '''import os,sys,json,base64,hashlib,stat,uuid,ast,glob,fnmatch,configparser,re,posixpath
r=json.load(sys.stdin); items=r["files"]; prepared=[]; replaced=[]; caches=[]; cleared=[]
ROOT="/mnt/UDISK/Uncurser"; migrations=[]; copied=[]; removed=[]; migration_temps=[]; retirements=[]; retired=[]
database=r.get("fluidd_category"); database_attempted=False; database_done=False
def fluidd(method="GET",value=None):
 import urllib.request,urllib.error
 payload=None if method=="GET" else json.dumps(dict(namespace="fluidd",key="macros",value=value)).encode()
 request=urllib.request.Request("http://127.0.0.1:7125/server/database/item?namespace=fluidd&key=macros",data=payload,headers={"Content-Type":"application/json"},method=method)
 opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
 try:
  with opener.open(request,timeout=5) as response: body=response.read(2097153)
 except urllib.error.HTTPError as error:
  if method=="GET" and error.code==404: return None
  raise
 if len(body)>2097152: raise ValueError("Fluidd macro preferences are too large")
 result=json.loads(body)
 if "error" in result: raise ValueError("Fluidd macro preferences: "+str(result["error"]))
 result=result.get("result",result)
 if method=="GET":
  if not isinstance(result,dict) or not isinstance(result.get("value"),dict): raise ValueError("Invalid Fluidd macro preferences")
  return result["value"]
def write_fluidd():
 global database_attempted,database_done
 if database is None or database_done: return
 if fluidd()!=database["expected"]: raise ValueError("Fluidd macro preferences changed; scan again")
 database_attempted=True
 fluidd("DELETE" if database["value"] is None else "POST",database["value"])
 if fluidd()!=database["value"]: raise ValueError("Saved Fluidd macro preferences could not be verified")
 database_done=True
def sha(data): return hashlib.sha256(data).hexdigest()
def read(path):
 try: s=os.lstat(path)
 except FileNotFoundError: return None
 if not stat.S_ISREG(s.st_mode): raise ValueError("Not a regular file: "+path)
 with open(path,"rb") as f: return f.read()
def fingerprint(data): return sha(data) if data is not None else None
def fileattrs(path):
 s=os.lstat(path)
 return dict(mode=stat.S_IMODE(s.st_mode),uid=s.st_uid,gid=s.st_gid)
def in_storage(path):
 try: return os.path.isabs(path) and os.path.normpath(path)==path and os.path.commonpath([ROOT,path])==ROOT
 except ValueError: return False
def storage_parent(path,create_missing=True):
 if not in_storage(path): return
 folders=[]; folder=os.path.dirname(path)
 while folder!=os.path.dirname(folder): folders.append(folder); folder=os.path.dirname(folder)
 folders.append(folder)
 for folder in reversed(folders):
  if not os.path.lexists(folder):
   if not in_storage(folder): raise ValueError("Missing storage parent: "+folder)
   if create_missing: os.mkdir(folder,0o700); syncdir(folder)
   else: continue
  if not stat.S_ISDIR(os.lstat(folder).st_mode): raise ValueError("Unsafe storage directory: "+folder)
def syncdir(path):
 fd=os.open(os.path.dirname(path),os.O_RDONLY)
 try: os.fsync(fd)
 finally: os.close(fd)
def create(path,data,attrs):
 storage_parent(path)
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
 for move in migrations: migration_guard(move)
 for item in retirements: retirement_guard(item)
 for item in prepared:
  if "expected_attrs" in item and not (item in replaced and item["new"] is None) and fileattrs(item["path"])!=item["expected_attrs"]: raise ValueError("Working file attributes changed; scan again: "+item["path"])
  if item.get("recovery_saved") and read(item["backup"])!=item["old"]: raise ValueError("The recovery copy changed: "+item["backup"])
 for path,expected in r.get("guards",{}).items():
  if any(item["path"]==path for item in retired): expected=None
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
def migration_guard(move,final=False):
 source=move["source"]; destination=move["destination"]
 if source in removed:
  if os.path.lexists(source): raise ValueError("Legacy metadata was recreated: "+source)
 elif fingerprint(read(source))!=move["source_hash"] or fileattrs(source)!=move["attrs"]: raise ValueError("Legacy metadata changed: "+source)
 expected=move["source_hash"]
 item=next((item for item in prepared if item["path"]==destination),None)
 if item is not None and (final or item in replaced): expected=item["new_hash"]
 saved=read(destination)
 if fingerprint(saved)!=expected or (saved is not None and fileattrs(destination)!=move["attrs"]): raise ValueError("Stored metadata changed: "+destination)
def firmware(value):
 value=value.strip() if isinstance(value,str) else ""
 if not re.fullmatch(r"[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+",value):
  versions=[part.partition(":")[2].strip() for part in value.split(";") if part.partition(":")[0].strip()=="DWIN sw ver" and ":" in part]
  value=versions[0] if len(versions)==1 else ""
 if not re.fullmatch(r"[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+",value): raise ValueError("Cannot verify original backup firmware")
 return tuple(map(int,value.split(".")))
def retirement_guard(item):
 path=item["path"]
 if item in retired:
  if os.path.lexists(path): raise ValueError("Retired original was recreated: "+path)
 elif fingerprint(read(path))!=item["sha256"] or (item["sha256"] is not None and fileattrs(path)!=item["attrs"]): raise ValueError("Old original backup changed since scan: "+path)
try:
 if database is not None:
  if not isinstance(database,dict) or set(database)!={"expected","value"} or any(value is not None and not isinstance(value,dict) for value in database.values()): raise ValueError("Invalid Fluidd category change")
  if fluidd()!=database["expected"]: raise ValueError("Fluidd macro preferences changed; scan again")
 # Retire only reserved original pairs matching their scanned state and reason.
 for entry in r.get("retire",[]):
  target=entry["target"]; backup=entry["backup"]; model=entry["model"]; release=entry.get("firmware"); reason=entry.get("reason","obsolete")
  if not isinstance(target,str) or not os.path.isabs(target) or os.path.normpath(target)!=target: raise ValueError("Invalid original retirement target")
  source="/mnt/UDISK/printer_data/config/Uncurser_mods.json" if target==os.path.join(ROOT,"Uncurser_mods.json") else target
  stem,suffix=os.path.splitext(source); marker={".py":".ptn",".cfg":".cfn",".js":".jfn",".json":".jsn"}.get(suffix)
  if marker is None: raise ValueError("Invalid original retirement type")
  canonical=os.path.normpath(ROOT+"/originals"+stem+marker+".Original.txt"); legacy=stem+marker+".Original.txt"
  if backup not in (canonical,legacy) or (in_storage(backup) and backup!=canonical): raise ValueError("Invalid original retirement path")
  if model!="F008" or r.get("model")!=model: raise ValueError("Original retirement model does not match")
  current=firmware(r.get("firmware"))
  files=entry["files"]
  if len(files)!=2 or {item["path"] for item in files}!={backup,backup+".json"}: raise ValueError("Original retirement requires a complete pair")
  pair=[]
  for item in files:
   path=item["path"]; attrs=item["attrs"]
   if any(path==old["path"] for old in retirements) or any(path==item["path"] for item in items) or any(path in (move["source"],move["destination"]) for move in r.get("moves",[])): raise ValueError("Overlapping original retirement")
   if item["sha256"] is None:
    if attrs is not None: raise ValueError("Invalid missing-original retirement attributes")
   elif not isinstance(attrs,dict) or set(attrs)!={"mode","uid","gid"} or any(type(value) is not int or value<0 for value in attrs.values()) or attrs["mode"]>0o7777: raise ValueError("Invalid original retirement attributes")
   storage_parent(path,False); data=read(path)
   if fingerprint(data)!=item["sha256"] or (data is not None and fileattrs(path)!=attrs): raise ValueError("Old original backup changed since scan: "+path)
   if any(fnmatch.fnmatchcase(path,pattern) for pattern in r.get("patterns",{})): raise ValueError("A config include matches an old original; review it before retirement")
   pair.append(dict(item,old=data))
  original=next(item["old"] for item in pair if item["path"]==backup)
  metadata=next(item["old"] for item in pair if item["path"]==backup+".json"); meta=None; version=None
  try:
   meta=json.loads(metadata) if metadata is not None else None
   valid=isinstance(meta,dict) and meta.get("path") in {target,source} and meta.get("model")==model and isinstance(meta.get("sha256"),str) and re.fullmatch(r"[0-9a-f]{64}",meta["sha256"]) and type(meta.get("absent",False)) is bool and all(type(meta.get(key)) is int and meta[key]>=0 for key in ("mode","uid","gid")) and meta["mode"]<=0o7777
   if valid: version=firmware(meta.get("firmware"))
  except (ValueError,TypeError,UnicodeError): valid=False
  complete=original is not None and metadata is not None and valid
  checksum=complete and meta["sha256"]!=sha(original)
  absence=complete and (not meta.get("absent") or (original==b"" and meta["sha256"]==sha(b"")))
  if reason=="obsolete":
   requested=firmware(release)
   if release!=".".join(map(str,requested)) or not complete or checksum or not absence or version!=requested or not version<current: raise ValueError("Only verified older firmware originals may be retired")
  elif reason=="checksum":
   if not complete or not checksum: raise ValueError("Original backup does not have a verified checksum failure")
  elif reason=="metadata":
   if complete and absence: raise ValueError("Original backup metadata is valid")
   if all(item["old"] is None for item in pair): raise ValueError("No original backup files to retire")
  else: raise ValueError("Unknown original retirement reason")
  retirements.extend(pair)
 # Validate the complete relocation before securing any new backup or original.
 for move in r.get("moves",[]):
  source=move["source"]; destination=move["destination"]; attributes=move["attrs"]
  if not in_storage(destination) or in_storage(source) or not os.path.isabs(source) or os.path.normpath(source)!=source: raise ValueError("Invalid metadata migration path")
  if any(source==old["source"] or destination==old["destination"] for old in migrations): raise ValueError("Repeated metadata migration")
  if set(attributes)!={"mode","uid","gid"} or any(type(value) is not int or value<0 for value in attributes.values()) or attributes["mode"]>0o7777: raise ValueError("Invalid metadata migration attributes")
  old=read(source); saved=read(destination)
  if old is None or fingerprint(old)!=move["source_hash"] or fileattrs(source)!=attributes: raise ValueError("Legacy metadata changed since scan: "+source)
  if fingerprint(saved)!=move["destination_hash"] or (saved is not None and (saved!=old or fileattrs(destination)!=attributes)): raise ValueError("Stored metadata conflicts with its legacy copy: "+destination)
  storage_parent(destination,False)
  migrations.append(dict(move,old=old))
 for move in migrations:
  source=move["source"]; destination=move["destination"]
  storage_parent(destination)
  if fingerprint(read(source))!=move["source_hash"] or fileattrs(source)!=move["attrs"]: raise ValueError("Legacy metadata changed before copying: "+source)
  if fingerprint(read(destination))!=move["destination_hash"]: raise ValueError("Stored metadata changed before copying: "+destination)
  if move["destination_hash"] is None:
   temporary=destination+".uncursed-pending-"+uuid.uuid4().hex+".txt"; migration_temps.append(temporary)
   create(temporary,move["old"],move["attrs"])
   if read(temporary)!=move["old"] or fileattrs(temporary)!=move["attrs"]: raise ValueError("Prepared metadata copy differs from its source")
   os.link(temporary,destination); copied.append(move); syncdir(destination)
  migration_guard(move)
 for item in items:
  p=item["path"]; storage_parent(p); old=read(p); s=os.stat(p if old is not None else os.path.dirname(p))
  if fingerprint(old)!=item["expected"]: raise ValueError("Working file changed since scan: "+p)
  new=base64.b64decode(item["data"],validate=True) if item["data"] is not None else None
  if fingerprint(new)!=item["new_hash"]: raise ValueError("Upload checksum mismatch")
  if new is None:
   if not item.get("original_absent") and not (item.get("recovery")=="records" and p==os.path.join(ROOT,"Uncurser_mods.json") and item["kind"]=="json"): raise ValueError("Only files originally absent may be removed")
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
   if p==os.path.join(ROOT,"Uncurser_mods.json") and (not isinstance(document,dict) or document.get("schema")!=1 or not isinstance(document.get("mods"),dict)): raise ValueError("Invalid mod records")
  else: raise ValueError("Unsupported file type")
  attrs=dict(mode=stat.S_IMODE(s.st_mode) if old is not None else 0o644,uid=s.st_uid,gid=s.st_gid)
  b=item["backup"]; m=b+".json"; meta=None
  if item.get("recovery"):
   if old is None or item.get("restore") or len(old)>2097152: raise ValueError("Invalid recovery operation")
   if item["recovery"] is True:
    if p!="/mnt/UDISK/printer_data/config/printer.cfg" or item["kind"]!="config" or new is None: raise ValueError("Invalid reference repair")
    prefix=base64.b64decode(item["removed_prefix"],validate=True)
    leading=base64.b64decode(item.get("leading_comments",""),validate=True); owners=set()
    for line in leading.splitlines(True):
     match=re.fullmatch(rb'[ \\t]*# UNCURSER[ \\t]+(\\{[^\\r\\n]*\\})\\r?\\n',line)
     if not match: raise ValueError("Invalid recovery metadata prefix")
     pairs=json.loads(match[1],object_pairs_hook=lambda values:values)
     if not isinstance(pairs,list) or len(pairs)!=3: raise ValueError("Invalid recovery metadata")
     metadata=dict(pairs)
     if set(metadata)!={"mod","version","firmware"}: raise ValueError("Invalid recovery metadata")
     owner=metadata["mod"]
     if not isinstance(owner,str) or not owner.startswith("/") or owner.startswith("//") or posixpath.normpath(owner)!=owner or "\\\\" in owner or any(ord(char)<32 or ord(char)==127 for char in owner) or owner in owners: raise ValueError("Invalid recovery metadata owner")
     if not isinstance(metadata["version"],str) or not re.fullmatch(r"\\d+\\.\\d+\\.\\d+",metadata["version"]) or not isinstance(metadata["firmware"],str) or not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\\.(?:0|[1-9][0-9]*)){3}",metadata["firmware"]): raise ValueError("Invalid recovery metadata version")
     owners.add(owner)
    blocks=[("# UNCURSER BEGIN bed_screws_v1\\n[include "+path+"]\\n# UNCURSER END bed_screws_v1\\n").encode() for path in ("screws_tilt_adjust.cfg","/usr/share/klipper/klippy/extras/screws_tilt_adjust.cfg")]
    if prefix.replace(b"\\r\\n",b"\\n") not in blocks or old.count(prefix)!=1 or not old.startswith(leading+prefix) or leading+old[len(leading)+len(prefix):]!=new: raise ValueError("Recovery changes more than the owned reference")
    expected_backup=os.path.join(ROOT,"recovery","printer."+item["expected"]+".txt")
   else:
    stem,suffix=os.path.splitext(p); markers={".py":".ptn",".cfg":".cfn",".js":".jfn",".json":".jsn"}
    if suffix not in markers or not os.path.isabs(p) or os.path.normpath(p)!=p: raise ValueError("Invalid recovery target")
    expected_backup=ROOT+"/recovery"+stem+markers[suffix]+"."+item["expected"]+".txt"
    if item["recovery"]=="stock":
     source=item["stock_source"]; expected=item["stock_hash"]
     if not source.startswith("/rom/") or os.path.normpath(source)!=source or source not in r.get("guards",{}) or r["guards"][source]!=expected: raise ValueError("Missing verified stock firmware guard")
     if item["kind"] not in ("python","config","javascript") or fingerprint(read(source))!=expected: raise ValueError("Stock firmware source changed")
     if new is None and (expected is not None or os.path.basename(p) not in ("screws_tilt_adjust.py","screws_tilt_adjust.cfg")): raise ValueError("Stock firmware does not prove this helper file absent")
    elif item["recovery"]=="records":
     if p!=os.path.join(ROOT,"Uncurser_mods.json") or item["kind"]!="json": raise ValueError("Invalid recovery record update")
     before=json.loads(old.decode("utf-8")); after=json.loads(new.decode("utf-8")) if new is not None else {"mods":{}}
     if not isinstance(before,dict) or before.get("schema")!=1 or not isinstance(before.get("mods"),dict): raise ValueError("Invalid existing mod records")
     allowed=item.get("record_targets",[])
     if not isinstance(allowed,list) or any(not isinstance(path,str) for path in allowed) or len(set(allowed))!=len(allowed): raise ValueError("Invalid record update scope")
     changed={path for path in before["mods"].keys()|after["mods"].keys() if before["mods"].get(path)!=after["mods"].get(path)}
     if changed!=set(allowed): raise ValueError("Record update changes an unrelated mod")
     if new is not None and after["mods"]==before["mods"]: raise ValueError("Record update has no changes")
    else: raise ValueError("Unknown recovery operation")
   if b!=expected_backup: raise ValueError("Invalid recovery backup path")
   item.setdefault("expected_attrs",attrs)
   storage_parent(b,False)
   if read(b) not in (None,old): raise ValueError("The existing recovery copy is damaged")
  elif os.path.lexists(b) or os.path.lexists(m):
   saved=read(b); meta=json.loads(read(m))
   original_paths={p}; aliases=set(item.get("original_paths",[]))
   if aliases:
    if p!=os.path.join(ROOT,"Uncurser_mods.json") or aliases-{p,"/mnt/UDISK/printer_data/config/Uncurser_mods.json"}: raise ValueError("Invalid original backup path aliases")
    original_paths.update(aliases)
   if saved is None or meta.get("path") not in original_paths or meta.get("sha256")!=sha(saved): raise ValueError("Original backup is damaged: "+b)
   if meta.get("absent",False)!=item.get("original_absent",False) or (meta.get("absent") and saved!=b""): raise ValueError("Original absence record is damaged")
   if meta["sha256"]!=item["original_hash"]: raise ValueError("Original backup changed since scan")
  elif item["original_hash"] is not None: raise ValueError("Original backup disappeared")
  elif not item["may_capture_original"]: raise ValueError("An initial original is required for this mod")
  item.update(old=old,new=new,attrs=attrs,meta=meta,tmp=p+".uncursed-pending-"+uuid.uuid4().hex+".txt")
  for pattern in r.get("patterns",{}):
   if old is None and item["kind"]!="config" and fnmatch.fnmatchcase(p,pattern): raise ValueError("A new non-config file would match a config include; review that include first")
   if any(fnmatch.fnmatchcase(path,pattern) for path in (b,m,item["tmp"])): raise ValueError("A backup or temporary file would match a config include; review that include first")
  prepared.append(item)
 # Verify the resulting include tree before removing helper dependencies,
 # including references in nested user configurations after an OTA reset.
 if r.get("verify_config"):
  root="/mnt/UDISK/printer_data/config/printer.cfg"; folder=os.path.dirname(root)
  planned={item["path"]:item["new"] for item in prepared}; visited=set()
  def visit(path):
   if path in visited or len(visited)>=64: raise ValueError("Repeated, circular or oversized configuration includes")
   if os.path.commonpath([folder,os.path.realpath(path)])!=folder and not (path=="/usr/share/klipper/klippy/extras/screws_tilt_adjust.cfg" and os.path.realpath(path)==path): raise ValueError("Config include is outside the supported directories")
   visited.add(path); data=planned[path] if path in planned else read(path)
   if data is None or len(data)>2097152: raise ValueError("Resulting configuration include is missing or oversized: "+path)
   if path not in planned:
    guard=r.setdefault("guards",{})
    if path in guard and guard[path]!=sha(data): raise ValueError("Included configuration changed: "+path)
    guard[path]=sha(data)
   for line in data.decode("utf-8").splitlines():
    match=re.fullmatch(r"\\s*\\[include ([^\\]]+)\\]\\s*(?:[#;].*)?",line)
    if not match: continue
    pattern=os.path.normpath(os.path.join(os.path.dirname(path),match.group(1).strip()))
    if any(fnmatch.fnmatchcase(move["source"],pattern) for move in migrations): raise ValueError("A config include matches a legacy backup; review it before moving backups")
    current=sorted(glob.glob(pattern)); patterns=r.setdefault("patterns",{})
    if pattern in patterns and patterns[pattern]!=current: raise ValueError("Included configuration list changed: "+pattern)
    patterns[pattern]=current
    matches=set(current)
    for target,data in planned.items():
     if fnmatch.fnmatchcase(target,pattern):
      if data is None: matches.discard(target)
      else: matches.add(target)
    if not matches: raise ValueError("Resulting configuration include has no matching files: "+pattern)
    for child in sorted(matches): visit(child)
  visit(root)
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
 # Every required backup is secured before any working file is replaced.
 for item in prepared:
  if item.get("recovery"):
   if read(item["backup"]) is None:
    draft=item["backup"]+".uncursed-pending-"+uuid.uuid4().hex+".txt"; migration_temps.append(draft)
    create(draft,item["old"],dict(item["attrs"],mode=0o444))
    try: os.link(draft,item["backup"])
    except FileExistsError:
     if read(item["backup"])!=item["old"]: raise ValueError("The existing recovery copy is damaged")
    syncdir(item["backup"])
   item["recovery_saved"]=True
   item["rollback"]=item["tmp"]+".rollback.txt"
   create(item["rollback"],item["old"],item["attrs"])
   if fingerprint(read(item["rollback"]))!=item["expected"]: raise ValueError("Prepared rollback checksum mismatch")
  elif item["meta"] is None:
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
  if item["path"]==os.path.join(ROOT,"Uncurser_mods.json"): write_fluidd()
  if fingerprint(read(item["path"]))!=item["expected"]: raise ValueError("Working file changed before replacement")
  if item["new"] is None: os.unlink(item["path"])
  elif item["old"] is None: os.link(item["tmp"],item["path"])
  else: os.replace(item["tmp"],item["path"])
  replaced.append(item); syncdir(item["path"])
  if os.path.exists(item["tmp"]): os.unlink(item["tmp"])
  if fingerprint(read(item["path"]))!=item["new_hash"]: raise ValueError("Working file changed after replacement")
 write_fluidd()
 if database is not None and fluidd()!=database["value"]: raise ValueError("Fluidd macro preferences changed during Apply")
 guards()
 for source in cache_sources:
  for path in cache_paths(source):
   if os.path.lexists(path): raise ValueError("Python cache was recreated during Apply; rescan: "+path)
 # Keep the old copies until working files and the last-written ledger verify.
 for item in prepared:
  if fingerprint(read(item["path"]))!=item["new_hash"]: raise ValueError("Working file changed before metadata cleanup: "+item["path"])
 for move in migrations: migration_guard(move,True)
 for move in migrations:
  for candidate in migrations: migration_guard(candidate,True)
  os.unlink(move["source"]); removed.append(move["source"]); syncdir(move["source"])
 for item in retirements:
  if item["old"] is None: continue
  guards()
  for candidate in prepared:
   if fingerprint(read(candidate["path"]))!=candidate["new_hash"]: raise ValueError("Working file changed before retiring originals: "+candidate["path"])
  os.unlink(item["path"]); retired.append(item); syncdir(item["path"])
 guards()
 print(json.dumps(dict(saved=[item["path"] for item in replaced],cleared=[item["path"] for item in cleared],moved=[dict(source=move["source"],destination=move["destination"]) for move in migrations],retired=[item["path"] for item in retired])))
except Exception as error:
 failures=[]
 if database_attempted:
  try:
   current=fluidd()
   if current==database["value"]:
    fluidd("DELETE" if database["expected"] is None else "POST",database["expected"])
    if fluidd()!=database["expected"]: raise ValueError("Fluidd rollback could not be verified")
   elif current!=database["expected"]: raise ValueError("Fluidd preferences changed by another writer")
  except Exception:
   failures.append("Fluidd macro preferences")
   # Keep the data-only undo proof if Moonraker becomes unavailable after
   # committing its write, even when file rollback has to remove the new ledger.
   try:
    proof=json.dumps(dict(change=database,records=next(item["new"].decode("utf-8") for item in prepared if item["path"]==os.path.join(ROOT,"Uncurser_mods.json") and item["new"] is not None))).encode()
    path=os.path.join(ROOT,"recovery","fluidd-category-"+sha(proof)+".txt")
    if read(path) is None: create(path,proof,dict(mode=0o600,uid=os.getuid(),gid=os.getgid()))
    failures.append(path)
   except Exception: failures.append("Fluidd category recovery proof could not be saved")
 # Originals remain available until all working changes and records commit.
 for item in reversed(retired):
  try:
   path=item["path"]; current=read(path)
   if current is not None:
    if current!=item["old"] or fileattrs(path)!=item["attrs"]: raise ValueError("retired original was recreated by another writer")
   else:
    temporary=path+".uncursed-pending-"+uuid.uuid4().hex+".txt"; migration_temps.append(temporary)
    create(temporary,item["old"],item["attrs"]); os.link(temporary,path); syncdir(path)
  except Exception: failures.append(item["path"])
 # Recreate already-removed legacy metadata before undoing normal file writes.
 for move in reversed(migrations):
  if move["source"] not in removed: continue
  try:
   source=move["source"]
   if os.path.lexists(source):
    if read(source)!=move["old"] or fileattrs(source)!=move["attrs"]: raise ValueError("legacy metadata was recreated by another writer")
   else:
    temporary=source+".uncursed-pending-"+uuid.uuid4().hex+".txt"; migration_temps.append(temporary)
    create(temporary,move["old"],move["attrs"]); os.link(temporary,source); syncdir(source)
  except Exception: failures.append(move["source"])
 for item in reversed(replaced):
  try:
   if fingerprint(read(item["path"]))!=item["new_hash"]: raise ValueError("changed by another writer")
   if "expected_attrs" in item and item["new"] is not None and fileattrs(item["path"])!=item["expected_attrs"]: raise ValueError("attributes changed by another writer")
   if os.path.exists(item["tmp"]): os.unlink(item["tmp"])
   if item["old"] is None: os.unlink(item["path"])
   else:
    temporary=item.get("rollback",item["tmp"])
    if "rollback" not in item: create(temporary,item["old"],item["attrs"])
    elif fingerprint(read(temporary))!=item["expected"]: raise ValueError("prepared rollback changed")
    if fingerprint(read(item["path"]))!=item["new_hash"]: raise ValueError("changed during rollback")
    if item["new"] is None: os.link(temporary,item["path"])
    else: os.replace(temporary,item["path"])
   syncdir(item["path"])
  except Exception: failures.append(item["path"])
 for item in reversed(cleared):
  try:
   if read(item["path"]) is not None: raise ValueError("cache recreated by another writer")
   create(item["tmp"],item["old"],item["attrs"])
   os.link(item["tmp"],item["path"]); syncdir(item["path"])
  except Exception: failures.append(item["path"])
 for move in reversed(copied):
  try:
   if read(move["source"])!=move["old"] or fileattrs(move["source"])!=move["attrs"]: raise ValueError("legacy metadata cannot be verified during rollback")
   if read(move["destination"])!=move["old"] or fileattrs(move["destination"])!=move["attrs"]: raise ValueError("stored metadata changed during rollback")
   os.unlink(move["destination"]); syncdir(move["destination"])
  except Exception: failures.append(move["destination"])
 if failures: raise ValueError("Apply was interrupted; some changes may remain. Rescan: "+", ".join(failures)) from error
 raise
finally:
 for item in prepared+caches:
  for temporary in (item["tmp"],item.get("rollback")):
   if temporary and os.path.exists(temporary): os.unlink(temporary)
 for temporary in migration_temps:
  if os.path.lexists(temporary): os.unlink(temporary)
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
