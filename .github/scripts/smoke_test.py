#!/usr/bin/env python3
"""End-to-end smoke test for the built mod jar. Used by CI and runnable locally.

    smoke_test.py server --jar JAR --work DIR [--no-mod]
    smoke_test.py client --jar JAR --work DIR [--no-mod] [--with-sodium-iris]

Unlike `gradlew runClient`, this loads the remapped release jar through the
production Fabric launcher, so it exercises exactly what players install.

server: boots a dedicated server, summons a portal, checks it survives, then
        records startup time, peak memory, idle CPU and tick time.
client: opens a world with the player standing over a portal to the Nether,
        screenshots the portal view, drops the player through it and requires a
        seamless (non-loading-screen) dimension change. The client needs a
        display; use `xvfb-run` or `gamescope --backend headless --`.

--no-mod measures the same scene without the mod, as a resource baseline.
Metrics are written to DIR/metrics.json. Exit code is non-zero on any failure.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
USER_AGENT = "immersive-portals-smoke-test"
FABRIC_MAVEN = "https://maven.fabricmc.net"
MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"

# Any of these in a log means the mod (or a mod it depends on) is broken.
FATAL_LOG = re.compile(
    r"MixinApplyError|MixinTransformerError|InvalidInjectionException"
    r"|InvalidMixinException|InjectionError|Mixin apply for mod .* failed"
    r"|NoSuchMethodError|NoSuchFieldError|NoClassDefFoundError"
    r"|OutOfMemoryError|Minecraft Crash Report|Incompatible mods found"
)

SERVER_BOOT_TIMEOUT = 300
IDLE_SETTLE_SECONDS = 10
IDLE_SAMPLE_SECONDS = 30
CLIENT_STAGE_TIMEOUT = 300
# Must stay under the gap between SMOKE_READY and SMOKE_RELEASE in DATAPACK (30 s).
CLIENT_SAMPLE_SECONDS = 20

# Budgets for the mod's cost over a mod-free baseline, enforced by `compare`.
MAX_SERVER_EXTRA_RSS_MB = 500
MAX_SERVER_EXTRA_STARTUP_S = 30
MAX_SERVER_IDLE_TICK_MS = 10
MAX_CLIENT_RSS_MB = 3500

DATAPACK = {
    "pack.mcmeta": json.dumps({"pack": {"pack_format": 48, "description": "smoke test"}}),
    "data/minecraft/tags/function/load.json": json.dumps({"values": ["smoke:load"]}),
    "data/minecraft/tags/function/tick.json": json.dumps({"values": ["smoke:tick"]}),
    "data/smoke/function/load.mcfunction": """
gamerule spawnRadius 0
gamerule doDaylightCycle false
gamerule doMobSpawning false
gamerule fallDamage false
time set noon
scoreboard objectives add smoke dummy
forceload add -16 -16 15 15
execute in minecraft:the_nether run forceload add -16 -16 15 15
schedule function smoke:setup 20t replace
""",
    "data/smoke/function/setup.mcfunction": """
setblock 0 -21 0 minecraft:glass
execute in minecraft:the_nether run fill -7 98 -7 7 119 7 minecraft:netherrack hollow
execute in minecraft:the_nether run fill -6 99 -6 6 99 6 minecraft:glowstone
execute unless entity @e[type=immersive_portals:portal] run function smoke:portal
""",
    # Horizontal 12x12 portal with its front facing up (axisW x axisH = +Y).
    "data/smoke/function/portal.mcfunction": (
        "summon immersive_portals:portal 0.5 -30 0.5 {width:12d,height:12d,thickness:0d,"
        "axisWX:0d,axisWY:0d,axisWZ:1d,axisHX:1d,axisHY:0d,axisHZ:0d,"
        'dimensionTo:"minecraft:the_nether",'
        "destinationX:0.5d,destinationY:108d,destinationZ:0.5d}\n"
    ),
    "data/smoke/function/tick.mcfunction": """
execute as @a[tag=!smoke_placed] run function smoke:place
execute if entity @a run scoreboard players add #t smoke 1
execute if score #t smoke matches 200 run say SMOKE_READY
execute if score #t smoke matches 800 run say SMOKE_RELEASE
execute if score #t smoke matches 800 run setblock 0 -21 0 minecraft:air
execute as @a[tag=!smoke_crossed] at @s if dimension minecraft:the_nether run function smoke:crossed
execute if score #d smoke matches 1.. run scoreboard players add #d smoke 1
execute if score #d smoke matches 200 run say SMOKE_DONE
""",
    "data/smoke/function/place.mcfunction": """
tp @s 0.5 -20 0.5 0 90
tag @s add smoke_placed
""",
    "data/smoke/function/crossed.mcfunction": """
tag @s add smoke_crossed
say SMOKE_CROSSED
scoreboard players set #d smoke 1
""",
}

CLIENT_OPTIONS = """\
pauseOnLostFocus:false
onboardAccessibility:false
skipMultiplayerWarnings:true
joinedFirstServer:true
tutorialStep:none
renderDistance:6
simulationDistance:6
maxFps:60
fov:0.5
narrator:0
soundCategory_master:0.0
"""


class SmokeFailure(Exception):
    pass


def log(message):
    print(f"[smoke] {message}", flush=True)


def gradle_properties():
    props = {}
    for line in (ROOT / "gradle.properties").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            props[key.strip()] = value.strip()
    return props


def fetch(url, dest, sha1=None):
    dest = Path(dest)
    if dest.exists() and (sha1 is None or hashlib.sha1(dest.read_bytes()).hexdigest() == sha1):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(request, timeout=120) as response, open(tmp, "wb") as out:
        shutil.copyfileobj(response, out)
    if sha1 and hashlib.sha1(tmp.read_bytes()).hexdigest() != sha1:
        tmp.unlink()
        raise SmokeFailure(f"checksum mismatch for {url}")
    tmp.replace(dest)
    return dest


def fetch_json(url, cache):
    name = hashlib.sha1(url.encode()).hexdigest() + ".json"
    return json.loads(fetch(url, Path(cache) / "json" / name).read_text())


def maven_path(coordinate):
    group, artifact, version = coordinate.split(":")[:3]
    return f"{group.replace('.', '/')}/{artifact}/{version}/{artifact}-{version}.jar"


def fabric_api_jar(props, cache):
    version = props["fabric_version"]
    path = f"net/fabricmc/fabric-api/fabric-api/{version}/fabric-api-{version}.jar"
    return fetch(f"{FABRIC_MAVEN}/{path}", Path(cache) / "mods" / f"fabric-api-{version}.jar")


def modrinth_jar(coordinate, cache):
    # gradle.properties stores these as maven.modrinth:<slug>:<version>
    _, slug, version = coordinate.split(":")
    name = f"{slug}-{version}.jar"
    url = f"https://api.modrinth.com/maven/maven/modrinth/{slug}/{version}/{name}"
    return fetch(url, Path(cache) / "mods" / name)


def fresh_dir(path):
    path = Path(path)
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def write_datapack(world):
    for rel, content in DATAPACK.items():
        target = world / "datapacks" / "smoke" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content.lstrip("\n"))


def proc_status(pid):
    """Returns (peak_rss_mb, cpu_seconds) for a running process."""
    status = Path(f"/proc/{pid}/status").read_text()
    peak_kb = int(re.search(r"VmHWM:\s+(\d+)", status).group(1))
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    ticks = int(fields[11]) + int(fields[12])
    return peak_kb // 1024, ticks / os.sysconf("SC_CLK_TCK")


def scan_for_fatal(*paths):
    for path in paths:
        if Path(path).exists():
            for line in Path(path).read_text(errors="replace").splitlines():
                if FATAL_LOG.search(line):
                    raise SmokeFailure(f"fatal line in {Path(path).name}: {line[:300]}")


def wait_for(pattern, log_path, proc, timeout, label):
    deadline = time.monotonic() + timeout
    regex = re.compile(pattern)
    while time.monotonic() < deadline:
        scan_for_fatal(log_path)
        if Path(log_path).exists():
            match = regex.search(Path(log_path).read_text(errors="replace"))
            if match:
                return match
        if proc.poll() is not None:
            raise SmokeFailure(f"process exited with {proc.returncode} waiting for {label}")
        time.sleep(1)
    raise SmokeFailure(f"timed out after {timeout}s waiting for {label}")


def stop_process(proc, timeout=60):
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def report_metrics(work, metrics):
    (Path(work) / "metrics.json").write_text(json.dumps(metrics, indent=2))
    log("metrics: " + json.dumps(metrics))


def run_server(args, props):
    work = fresh_dir(args.work)
    mods = work / "mods"
    mods.mkdir()
    installer = fetch_json("https://meta.fabricmc.net/v2/versions/installer", args.cache)[0]["version"]
    launcher = fetch(
        f"https://meta.fabricmc.net/v2/versions/loader/{props['minecraft_version']}"
        f"/{props['loader_version']}/{installer}/server/jar",
        Path(args.cache) / f"fabric-server-{props['minecraft_version']}-{props['loader_version']}.jar",
    )
    shutil.copy(fabric_api_jar(props, args.cache), mods)
    if not args.no_mod:
        shutil.copy(args.jar, mods)

    (work / "eula.txt").write_text("eula=true\n")
    (work / "server.properties").write_text(
        "level-type=minecraft\\:flat\nlevel-seed=1\nonline-mode=false\ndifficulty=peaceful\n"
        "view-distance=6\nsimulation-distance=6\nspawn-monsters=false\n"
        "spawn-animals=false\nspawn-npcs=false\nserver-port=0\n"
    )
    world = work / "world"
    world.mkdir()
    write_datapack(world)

    log_path = work / "server.log"
    started = time.monotonic()
    with open(log_path, "w") as out:
        proc = subprocess.Popen(
            [args.java, "-Xmx2G", "-jar", str(launcher), "nogui"],
            cwd=work, stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT, text=True,
        )

    def send(command):
        proc.stdin.write(command + "\n")
        proc.stdin.flush()

    try:
        wait_for(r"\bDone \(\d", log_path, proc, SERVER_BOOT_TIMEOUT, "server startup")
        startup = time.monotonic() - started
        log(f"server started in {startup:.1f}s")
        time.sleep(IDLE_SETTLE_SECONDS)

        if not args.no_mod:
            # Checked after several seconds of ticking: invalid portals remove themselves.
            send("execute if entity @e[type=immersive_portals:portal]")
            wait_for(r"Test passed, count: 1", log_path, proc, 30, "the portal entity to exist")
            log("portal entity exists and survived ticking")
            send("help portal")
            wait_for(r"/portal ", log_path, proc, 30, "the /portal command")

        _, cpu_before = proc_status(proc.pid)
        time.sleep(IDLE_SAMPLE_SECONDS)
        peak_mb, cpu_after = proc_status(proc.pid)
        send("tick query")
        tick = wait_for(r"Average time per tick: ([\d.]+)ms", log_path, proc, 30, "tick stats")

        send("stop")
        if proc.wait(120) != 0:
            raise SmokeFailure(f"server exited with {proc.returncode} on stop")
    finally:
        stop_process(proc)

    scan_for_fatal(log_path, work / "logs" / "latest.log")
    if (work / "crash-reports").exists() and list((work / "crash-reports").glob("*")):
        raise SmokeFailure("server wrote a crash report")

    report_metrics(work, {
        "mode": "server", "mod": not args.no_mod,
        "startup_s": round(startup, 1),
        "peak_rss_mb": peak_mb,
        "idle_cpu_cores": round((cpu_after - cpu_before) / IDLE_SAMPLE_SECONDS, 3),
        "idle_tick_ms": float(tick.group(1)),
    })
    return work


def client_classpath(props, cache):
    version = props["minecraft_version"]
    manifest = fetch_json(MANIFEST, cache)
    entry = next(v for v in manifest["versions"] if v["id"] == version)
    meta = json.loads(fetch(entry["url"], Path(cache) / "versions" / f"{version}.json", entry["sha1"]).read_text())
    client = meta["downloads"]["client"]
    client_jar = fetch(client["url"], Path(cache) / "versions" / f"{version}.jar", client["sha1"])

    libraries = {}
    for lib in meta["libraries"]:
        rules = lib.get("rules", [])
        allowed = not rules or any(
            r["action"] == "allow" and r.get("os", {}).get("name", "linux") == "linux" for r in rules
        ) and not any(
            r["action"] == "disallow" and r.get("os", {}).get("name") == "linux" for r in rules
        )
        artifact = lib["downloads"].get("artifact")
        if allowed and artifact:
            key = ":".join(lib["name"].split(":")[:2] + lib["name"].split(":")[3:])
            libraries[key] = fetch(artifact["url"], Path(cache) / "libraries" / artifact["path"], artifact["sha1"])

    # Fabric's libraries win over vanilla's on a clash, as in the real launcher.
    profile = fetch_json(
        f"https://meta.fabricmc.net/v2/versions/loader/{version}/{props['loader_version']}/profile/json", cache
    )
    for lib in profile["libraries"]:
        path = maven_path(lib["name"])
        base = lib.get("url", FABRIC_MAVEN).rstrip("/")
        key = ":".join(lib["name"].split(":")[:2])
        libraries[key] = fetch(f"{base}/{path}", Path(cache) / "libraries" / path, lib.get("sha1"))

    return [str(p) for p in libraries.values()] + [str(client_jar)], profile["mainClass"], meta["assetIndex"]["id"]


def screenshot(game, dest):
    """Presses Minecraft's own screenshot key, so this is the real framebuffer."""
    folder = game / "screenshots"
    before = set(folder.glob("*.png")) if folder.exists() else set()
    window = subprocess.run(
        ["xdotool", "search", "--name", "Minecraft"], check=True, capture_output=True, text=True, timeout=30,
    ).stdout.split()[-1]
    subprocess.run(["xdotool", "key", "--window", window, "F2"], check=True, timeout=30)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        new = set(folder.glob("*.png")) - before if folder.exists() else set()
        if new:
            time.sleep(1)  # Saved on a background thread; let the write finish.
            shutil.copy(new.pop(), dest)
            return
        time.sleep(0.5)
    raise SmokeFailure("the game did not save a screenshot")


def mean_color(path, crop):
    """Average (r, g, b) of the crop, given as an ffmpeg `w:h:x:y` expression."""
    raw = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", str(path), "-vf", f"crop={crop},scale=1:1",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        check=True, capture_output=True, timeout=60,
    ).stdout
    return tuple(raw[:3])


def run_client(args, props):
    # The world comes from a server run so the portal and terrain are the same each time.
    server_args = argparse.Namespace(**{**vars(args), "work": str(Path(args.work) / "server")})
    work = Path(args.work)
    if work.exists():
        shutil.rmtree(work)
    server_work = run_server(server_args, props)

    game = fresh_dir(work / "game")
    shutil.copytree(server_work / "world", game / "saves" / "smoke")
    (game / "options.txt").write_text(CLIENT_OPTIONS)
    (game / "config").mkdir()
    (game / "config" / "immersive_portals.json").write_text('{"initialScreenShown": true}')
    mods = game / "mods"
    mods.mkdir()
    shutil.copy(fabric_api_jar(props, args.cache), mods)
    if not args.no_mod:
        shutil.copy(args.jar, mods)
    if args.with_sodium_iris:
        shutil.copy(modrinth_jar(props["sodium_path"], args.cache), mods)
        shutil.copy(modrinth_jar(props["iris_path"], args.cache), mods)

    classpath, main_class, asset_index = client_classpath(props, args.cache)
    log_path = game / "logs" / "latest.log"
    command = [
        args.java, "-Xmx3G", "-cp", os.pathsep.join(classpath), main_class,
        "--username", "SmokeTest", "--version", props["minecraft_version"],
        "--gameDir", str(game), "--assetsDir", str(game / "assets"), "--assetIndex", asset_index,
        "--accessToken", "0", "--userType", "legacy", "--versionType", "release",
        "--width", "854", "--height", "480", "--quickPlaySingleplayer", "smoke",
    ]
    with open(work / "client.out", "w") as out:
        proc = subprocess.Popen(command, cwd=game, stdout=out, stderr=subprocess.STDOUT, env=os.environ)

    try:
        wait_for(r"SMOKE_READY", log_path, proc, CLIENT_STAGE_TIMEOUT, "the player to load into the world")
        log("player in world, standing over the portal")
        _, cpu_before = proc_status(proc.pid)
        time.sleep(CLIENT_SAMPLE_SECONDS)
        peak_mb, cpu_after = proc_status(proc.pid)
        cpu_cores = (cpu_after - cpu_before) / CLIENT_SAMPLE_SECONDS

        # Taken last so chunks have appeared even on a slow software renderer.
        screenshot(game, work / "portal-view.png")
        if not args.no_mod:
            # A band of the Nether wall seen through the portal; without a
            # working portal view this part of the screen is overworld grass.
            r, g, b = mean_color(work / "portal-view.png", "iw*0.07:ih*0.06:iw*0.465:ih*0.26")
            log(f"colour through the portal: rgb({r}, {g}, {b})")
            if r < g * 1.5:
                raise SmokeFailure("the Nether is not visible through the portal")

        if not args.no_mod:
            wait_for(r"SMOKE_RELEASE", log_path, proc, CLIENT_STAGE_TIMEOUT, "the release of the player")
            wait_for(r"SMOKE_CROSSED", log_path, proc, 60, "the player to cross the portal")
            wait_for(r"Client Teleported", log_path, proc, 10, "the client-side seamless teleport")
            log("player crossed into the Nether through the portal")
            wait_for(r"SMOKE_DONE", log_path, proc, CLIENT_STAGE_TIMEOUT, "the player to stay in the Nether")
            screenshot(game, work / "after-crossing.png")
            peak_mb, _ = proc_status(proc.pid)
    finally:
        stop_process(proc)
    if proc.returncode not in (0, 143, -signal.SIGTERM):
        raise SmokeFailure(f"client exited abnormally with {proc.returncode}")

    scan_for_fatal(log_path, work / "client.out")
    if (game / "crash-reports").exists() and list((game / "crash-reports").glob("*")):
        raise SmokeFailure("client wrote a crash report")

    report_metrics(work, {
        "mode": "client", "mod": not args.no_mod, "sodium_iris": args.with_sodium_iris,
        "peak_rss_mb": peak_mb, "cpu_cores_at_60fps_cap": round(cpu_cores, 3),
    })


def compare(args):
    """Fails when the mod costs more than the budgets above; prints a summary table."""
    with_mod = json.loads(Path(args.with_mod).read_text())
    baseline = json.loads(Path(args.baseline).read_text()) if args.baseline else None
    rows = []
    failures = []
    for key, value in with_mod.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            rows.append(f"| {key} | {baseline.get(key, '-') if baseline else '-'} | {value} |")
    if with_mod["mode"] == "server":
        if with_mod["peak_rss_mb"] - baseline["peak_rss_mb"] > MAX_SERVER_EXTRA_RSS_MB:
            failures.append(f"server memory overhead above {MAX_SERVER_EXTRA_RSS_MB} MB")
        if with_mod["startup_s"] - baseline["startup_s"] > MAX_SERVER_EXTRA_STARTUP_S:
            failures.append(f"server startup overhead above {MAX_SERVER_EXTRA_STARTUP_S} s")
        if with_mod["idle_tick_ms"] > MAX_SERVER_IDLE_TICK_MS:
            failures.append(f"idle tick above {MAX_SERVER_IDLE_TICK_MS} ms")
    elif with_mod["peak_rss_mb"] > MAX_CLIENT_RSS_MB:
        failures.append(f"client memory above {MAX_CLIENT_RSS_MB} MB")

    table = f"### {args.title}\n\n| metric | no mod | with mod |\n|---|---|---|\n" + "\n".join(rows) + "\n"
    print(table)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write(table + "\n")
    if failures:
        raise SmokeFailure("; ".join(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)
    for mode in ("server", "client"):
        p = sub.add_parser(mode)
        p.add_argument("--jar", help="mod jar to test (required unless --no-mod)")
        p.add_argument("--work", required=True, help="scratch directory; deleted and recreated")
        p.add_argument("--cache", default=str(Path.home() / ".cache" / "immersive-portals-smoke"))
        p.add_argument("--java", default=shutil.which("java") or "java")
        p.add_argument("--no-mod", action="store_true", help="measure a mod-free baseline")
        if mode == "client":
            p.add_argument("--with-sodium-iris", action="store_true")
    p = sub.add_parser("compare")
    p.add_argument("--with-mod", required=True)
    p.add_argument("--baseline")
    p.add_argument("--title", default="Resource use")
    args = parser.parse_args()

    try:
        if args.mode == "compare":
            compare(args)
        else:
            if not args.no_mod and not args.jar:
                parser.error("--jar is required unless --no-mod is set")
            props = gradle_properties()
            if args.mode == "server":
                run_server(args, props)
            else:
                run_client(args, props)
    except SmokeFailure as failure:
        log(f"FAILED: {failure}")
        return 1
    log("PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
