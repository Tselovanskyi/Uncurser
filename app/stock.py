"""Read current firmware originals without treating modified files as backups."""
import base64
import hashlib
import re

from . import fluidd_screws, mesh, mesh_temperature, mods, network, screws, smoothing


INIT = "/rom/etc/init.d/klipper"
_TOOLS = ("/usr/bin/get_sn_mac.sh", "/usr/bin/keybox", "/usr/sbin/fw_printenv", "/etc/fw_env.config")
_CONFIGS = ("printer.cfg", "gcode_macro.cfg", "sensorless.cfg", "screws_tilt_adjust.cfg")
_COMMON = (mods.TARGET, smoothing.HOMING.TARGET, screws.PYTHON, screws.CONFIG,
           fluidd_screws.TARGET, fluidd_screws.INDEX)

# Fixed, read-only queries travel through Remote's existing in-memory bootstrap.
READ = '''import os,sys,json,base64,hashlib,stat,subprocess,re
r=json.load(sys.stdin); files={}; limit=2097152; total=0
romdev=os.stat("/rom").st_dev
def read(path):
 global total
 if not path.startswith("/rom/") and path not in r["tools"]+["/etc/init.d/klipper"]: raise ValueError("Unexpected firmware source")
 if os.path.normpath(path)!=path or os.path.realpath(path)!=path: raise ValueError("Firmware source follows a link: "+path)
 parent=path
 while not os.path.lexists(parent): parent=os.path.dirname(parent)
 if path.startswith("/rom/") and os.stat(parent).st_dev!=romdev: raise ValueError("Firmware source is not on ROM: "+path)
 try: s=os.lstat(path)
 except FileNotFoundError: files[path]=None; return None
 if not stat.S_ISREG(s.st_mode) or s.st_size>limit: raise ValueError("Unsafe firmware source: "+path)
 with open(path,"rb") as f: data=f.read(limit+1)
 total+=len(data)
 if len(data)>limit or total>33554432: raise ValueError("Firmware sources exceed supported size")
 files[path]=dict(data=base64.b64encode(data).decode(),sha256=hashlib.sha256(data).hexdigest())
 return data
def proc(path):
 with open(path) as f: data=f.read(262145)
 if len(data)>262144: raise ValueError("Unexpected boot metadata size")
 return data
def command(args):
 return subprocess.check_output(args,stderr=subprocess.PIPE,timeout=12).decode("utf-8").strip()
def device(path):
 s=os.stat(path)
 if not stat.S_ISBLK(s.st_mode): raise ValueError("Boot root is not a block device")
 return str(os.major(s.st_rdev))+":"+str(os.minor(s.st_rdev))
mountinfo=proc("/proc/self/mountinfo"); cmdline=proc("/proc/cmdline")
roots=re.findall(r"(?:^|\\s)root=(/dev/[^\\s]+)",cmdline)
if len(roots)!=1: raise ValueError("Cannot identify the boot root device")
boot_device=device(roots[0])
mounts=[(line.split(" - ")[0].split(),line.split(" - ")[1].split()) for line in mountinfo.splitlines()]
rom=[item for item in mounts if item[0][4]=="/rom"]; active=[item for item in mounts if item[0][4]=="/"]
if len(rom)!=1 or len(active)!=1: raise ValueError("Cannot identify the active firmware mounts")
rf,rs=rom[0]; af,as_=active[0]
if rs[0]!="squashfs" or "ro" not in rf[5].split(",") or "ro" not in rs[2].split(",") or rf[2]!=boot_device or rf[2]!=str(os.major(romdev))+":"+str(os.minor(romdev)): raise ValueError("ROM is not the active read-only boot image")
if as_[0] not in ("overlay","overlayfs") or not any(value in ("lowerdir=/","lowerdir=/rom") for value in as_[2].split(",")): raise ValueError("Active root does not use the verified ROM")
for path in r["tools"]+["/etc/init.d/klipper"]:
 if read(path) is None or read("/rom"+path)!=base64.b64decode(files[path]["data"]): raise ValueError("Firmware identity tool differs from ROM: "+path)
hardware={field:command(["/usr/bin/get_sn_mac.sh",field]) for field in ("model","board","structure_version","pcba_test")}
environment={key:command(["/usr/sbin/fw_printenv","-n",key]) for key in ("version","root_partition")}
if not re.fullmatch(r"rootfs[A-Za-z0-9_]*",environment["root_partition"]): raise ValueError("Unknown active firmware partition")
environment["root_device"]=device("/dev/by-name/"+environment["root_partition"])
init=base64.b64decode(files["/rom/etc/init.d/klipper"]["data"]).decode("utf-8-sig")
variants=sorted(set(re.findall(r"^\\s*config_dir=(F008_[A-Za-z0-9_]+)\\s*$",init,re.M)))
if not variants or len(variants)>8: raise ValueError("Unrecognized firmware config selection")
directories={}
for variant in variants:
 folder="/rom/usr/share/klipper/config/"+variant
 directories[variant]=os.path.realpath(folder)==folder and os.path.isdir(folder) and os.stat(folder).st_dev==romdev
 if directories[variant]:
  for name in r["configs"]: read(folder+"/"+name)
for path in r["common"]: read("/rom"+path)
print(json.dumps(dict(files=files,hardware=hardware,environment=environment,mountinfo=mountinfo,cmdline=cmdline,directories=directories,
 boot_device=boot_device,rom_device=str(os.major(romdev))+":"+str(os.minor(romdev)))))
'''


def select_variant(init, board, structure):
    """Accept only the board/structure mapping expressed by this ROM's init."""
    init = init.replace("\r\n", "\n")
    bodies = re.findall(r'copy_config\(\)\s*\{\n(.*?)\n\}', init, re.S)
    if len(bodies) != 1:
        raise ValueError("Unrecognized firmware configuration selector.")
    cases = re.findall(r'"F008"\)\s*\n(.*?)\n\s*;;', bodies[0], re.S)
    if len(cases) != 1:
        raise ValueError("The ROM has no unique K2 Plus configuration mapping.")
    branch = "\n".join(line.strip() for line in cases[0].splitlines()
                       if line.strip() and not line.lstrip().startswith("#"))
    rule = re.compile(r'(?:if|elif) \[ "\$board" = "([A-Za-z0-9_]+)" -a '
                      r'"x\$structure_version" = "x([0-9]+)" \]; then\n'
                      r'config_dir=(F008_[A-Za-z0-9_]+)\n')
    matches = rule.findall(branch + "\n")
    remainder = rule.sub("", branch + "\n")
    if not matches or remainder != "else\nconfig_dir=\nfi\ngcode_dir=F008\n":
        raise ValueError("The ROM's K2 Plus selection rules require review.")
    identities = [(item[0], item[1]) for item in matches]
    if len(set(identities)) != len(identities):
        raise ValueError("The ROM's hardware selection rules overlap.")
    selected = [variant for candidate, version, variant in matches if (candidate, version) == (board, structure)]
    if len(selected) != 1:
        raise ValueError("The ROM has no configuration for this board and structure version.")
    return selected[0]


def _mount_proof(raw):
    entries = []
    for line in raw["mountinfo"].splitlines():
        fields, separator, tail = line.partition(" - ")
        fields, tail = fields.split(), tail.split()
        if not separator or len(fields) < 6 or len(tail) < 3:
            raise ValueError("Invalid firmware mount metadata.")
        entries.append((fields[4], fields[2], fields[5].split(","), tail[0], tail[2].split(",")))
    rom = [item for item in entries if item[0] == "/rom"]
    root = [item for item in entries if item[0] == "/"]
    if (len(rom) != 1 or len(root) != 1 or rom[0][3] != "squashfs"
            or "ro" not in rom[0][2] or "ro" not in rom[0][4]
            or root[0][3] not in ("overlay", "overlayfs")
            or not any(value in ("lowerdir=/", "lowerdir=/rom") for value in root[0][4])
            or rom[0][1] != raw["rom_device"] or rom[0][1] != raw["boot_device"]
            or rom[0][1] != raw["environment"]["root_device"]):
        raise ValueError("The read-only ROM could not be matched to the active boot root.")
    return {"rom_device": rom[0][1], "rom_type": "squashfs", "rom_readonly": True,
            "root_type": root[0][3], "lowerdir": next(value for value in root[0][4] if value.startswith("lowerdir="))}


def scan(remote, status):
    """Return guarded current-ROM baselines, or a reason no stock proof is safe."""
    result = {"available": False, "reason": "", "identity": {}, "files": {}, "guards": {}, "proof": {}}
    try:
        network.require_removal_supported(status)
        raw = remote.python(READ, {"tools": list(_TOOLS), "configs": list(_CONFIGS), "common": list(_COMMON)},
                            max_output_bytes=48 * 1024 * 1024)
        mount = _mount_proof(raw)
        model, firmware = network.device_identity(status)
        hardware, environment = raw["hardware"], raw["environment"]
        if hardware["model"] != model or environment["version"] != firmware or hardware["pcba_test"] != "0":
            raise ValueError("ROM firmware or hardware identity does not match this printer.")
        if (not re.fullmatch(r"CR[A-Z0-9]+", hardware["board"])
                or not re.fullmatch(r"[0-9]+", hardware["structure_version"])):
            raise ValueError("The printer board or structure version could not be verified.")
        records = {}
        for path, value in raw["files"].items():
            if value is None:
                records[path] = None
                continue
            data = base64.b64decode(value["data"], validate=True)
            if len(data) > 2097152 or hashlib.sha256(data).hexdigest() != value["sha256"]:
                raise ValueError("A ROM source checksum could not be verified: " + path)
            records[path] = dict(data=data, sha256=value["sha256"], source=path)
        for path in (*_TOOLS, "/etc/init.d/klipper"):
            if not records.get(path) or not records.get("/rom" + path) or records[path]["data"] != records["/rom" + path]["data"]:
                raise ValueError("A firmware identity source does not match ROM: " + path)
        variant = select_variant(records[INIT]["data"].decode("utf-8-sig"), hardware["board"], hardware["structure_version"])
        if raw["directories"].get(variant) is not True:
            raise ValueError("The selected stock configuration directory is unavailable on ROM.")
        sources = {path: "/rom" + path for path in _COMMON}
        folder = "/rom/usr/share/klipper/config/" + variant + "/"
        sources.update({mesh.TARGET: folder + "printer.cfg", mesh_temperature.TARGET: folder + "gcode_macro.cfg",
                        smoothing.TARGET: folder + "sensorless.cfg", screws.LEGACY_CONFIG: folder + "screws_tilt_adjust.cfg"})
        files = {}
        for path, source in sources.items():
            if source not in records:
                raise ValueError("The selected ROM source was not scanned: " + source)
            value = records[source]
            absent = value is None and path in (screws.PYTHON, screws.CONFIG, screws.LEGACY_CONFIG)
            files[path] = dict(value or dict(data=None, sha256=None, source=source),
                               available=value is not None or absent, absent=absent,
                               reason="" if value is not None or absent else "The current ROM file is unavailable.")
        index = files[fluidd_screws.INDEX]["data"]
        if index is None or index.count(('src="./' + fluidd_screws.ASSET + '"').encode()) != 1:
            files[fluidd_screws.TARGET].update(available=False, reason="The current ROM uses a different Fluidd interface.")
        guards = {source: records[source]["sha256"] if records[source] else None for source in sources.values()}
        guards.update({path: records[path]["sha256"] for path in (*_TOOLS, "/etc/init.d/klipper",
                       *("/rom" + path for path in (*_TOOLS, "/etc/init.d/klipper")))})
        guards.update({"/proc/self/mountinfo": hashlib.sha256(raw["mountinfo"].encode()).hexdigest(),
                       "/proc/cmdline": hashlib.sha256(raw["cmdline"].encode()).hexdigest()})
        identity = dict(model=model, firmware=firmware, board=hardware["board"],
                        structure_version=hardware["structure_version"], variant=variant)
        result.update(available=True, identity=identity, files=files, guards=guards,
                      proof=dict(mount=mount, environment=environment, hardware=hardware, identity=identity))
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, RuntimeError, OSError) as error:
        result["reason"] = str(error) or type(error).__name__
    return result
