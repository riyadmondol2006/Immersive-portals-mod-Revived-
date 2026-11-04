# Immersive Portals Mod (Revived)

A Minecraft mod that provides see-through portals and seamless teleportation. It can also create "Non-Euclidean" space effects.

![immptl.png](https://i.loli.net/2021/09/30/chHMG45dsnZNqep.png)

This is a community fork. The [original project](https://github.com/iPortalTeam/ImmersivePortalsMod) by qouteall has been archived and is no longer maintained.

**[Download the latest build](https://github.com/riyadmondol2006/Immersive-portals-mod-Revived-/releases/latest)**

## Compatibility

| Minecraft | Status |
|---|---|
| 1.21, 1.21.1 | Supported |
| 1.21.2 to 1.21.11 | Not supported |
| 26.1 to 26.3 (latest) | Not supported, see [below](#why-it-does-not-run-on-the-latest-minecraft) |

To install, put these in your `mods` folder with [Fabric Loader](https://fabricmc.net/use/) on Minecraft 1.21.1:

* The mod jar from [Releases](https://github.com/riyadmondol2006/Immersive-portals-mod-Revived-/releases)
* [Fabric API](https://modrinth.com/mod/fabric-api) 0.109.0 or newer

[Sodium](https://modrinth.com/mod/sodium) and [Iris](https://modrinth.com/mod/iris) are optional, but only these exact versions load: Sodium `0.6.0` and Iris `1.8.0`.

This is the Fabric version. There is no maintained NeoForge version of this fork.

## Why it does not run on the latest Minecraft

The mod builds and runs correctly on 1.21.1, but Minecraft has changed too much since then. Trying the 1.21.1 jar on 26.3 fails at three separate levels, and fixing one only reveals the next:

1. **Version check.** Fabric refuses to start because the mod and DimLib both declare `minecraft: 1.21 or 1.21.1`.
2. **Obfuscation.** Minecraft 26.1 and later ship unobfuscated, so jars built against the old `intermediary` names are rejected: `Namespace (intermediary) does not match current runtime namespace (official)`. Moving past this needs Java 25, Gradle 9, the `net.fabricmc.fabric-loom` plugin, and new builds of every bundled mod.
3. **Source code.** Compiling against 26.3 gives at least 1,843 errors across 219 files. Each error can hide others, so the real number is higher.

| Area | Compile errors | Main cause |
|---|---:|---|
| General code and helpers | 845 | Renames such as `ResourceLocation`, changed `ChunkPos` and NBT APIs |
| Portal rendering | 382 | Rendering rewrite: `ShaderInstance`, `RenderType`, `VertexBuffer`, `GlStateManager` replaced or removed |
| Mixins | 276 | Target classes and methods changed |
| Commands | 136 | Portal commands use the changed APIs above |
| Chunk loading and networking | 114 | Chunk system internals changed |
| Sodium and Iris compatibility | 90 | Both rewrote the internals this code hooks into |

The compiler can't see everything. Most of the mod's 181 mixins locate their targets by name, so about 300 injection points need checking at runtime as well. Its stencil and framebuffer code also calls OpenGL directly, and Mojang [plans to replace OpenGL with Vulkan](https://www.neowin.net/news/minecraft-java-edition-is-upgrading-to-vulkan-but-its-not-great-news-for-mods/). [DimLib](https://github.com/iPortalTeam/DimLib), a required library, has no official release newer than 1.21.1 either, so it needs porting or replacing too.

A port is a large rewrite of the renderer rather than a version bump. The [1.21.3 work-in-progress branch](https://github.com/iPortalTeam/ImmersivePortalsMod/tree/1.21.3) is as far as the original project got.

## Resource use

Measured on Minecraft 1.21.1 in the automated test scene: render distance 6, one active portal looking into the Nether, 60 FPS cap.

| | Without the mod | With the mod | With the mod, Sodium and Iris |
|---|---:|---:|---:|
| Client peak memory | 1,315 to 1,374 MB | 1,524 to 1,586 MB | 1,413 to 1,489 MB |
| Client CPU | 0.23 cores | 0.54 to 0.63 cores | 0.43 cores |

On a dedicated server with no players online, the mod makes no measurable difference: about 870 MB peak memory, 0.3 ms per tick and 16 s startup, with or without it.

These numbers come from short runs (about a minute in-game) with one portal, so they don't show long-session behaviour.

Costs grow with what portals show, because each visible portal renders its destination and keeps those chunks loaded. Allocate more than 2 GB of memory to the game, or the mod shows a low-memory warning. If you're short on memory or frame rate, lower the render distance first.

## Releases

Every push runs [this workflow](.github/workflows/release.yml), and a release is only created if all of it passes:

1. Build the jar and run the unit tests.
2. Start a real Fabric server with the jar, summon a portal, and check it stays alive.
3. Start a real Fabric client, with and without Sodium and Iris. It checks that the Nether is visible through a portal, then drops the player through and checks for a seamless dimension change.
4. Compare resource use with a mod-free baseline and fail if it exceeds the [budgets](.github/scripts/smoke_test.py).
5. Publish the jar and its checksum as a [GitHub release](https://github.com/riyadmondol2006/Immersive-portals-mod-Revived-/releases).

Pushes to the default branch become the latest release, and other branches become pre-releases. Add `[skip ci]` to a commit message to skip a run. Screenshots and logs from each run are attached to it as the `smoke-test-evidence` artifact.

## Building and testing

You need JDK 21.

```sh
./gradlew build
```

The jar is written to `build/libs/`. To run the same smoke tests as the release workflow (needs Python 3, `xdotool` and `ffmpeg`):

```sh
JAR=$(ls build/libs/*-fabric.jar)
python3 .github/scripts/smoke_test.py server --jar "$JAR" --work /tmp/smoke-server
xvfb-run -a -s "-screen 0 854x480x24" python3 .github/scripts/smoke_test.py client --jar "$JAR" --work /tmp/smoke-client
```

If you don't have `xvfb-run`, `gamescope --backend headless -W 854 -H 480 -w 854 -h 480 --` works in its place, but it hides the exit code, so look for `[smoke] PASSED` in the output. For general mod development setup, see the [Fabric documentation](https://docs.fabricmc.net/develop/).

## API

This mod also provides an API for:

* Managing see-through portals
* Dynamically adding dimensions
* Synchronizing remote chunks to the client
* Rendering the world into a GUI
* Other utilities

See the [API description](https://qouteall.fun/immptl/wiki/API-for-Other-Mods.html) and [implementation details](https://qouteall.fun/immptl/wiki/Implementation-Details).

## Credits

Created by [qouteall](https://www.patreon.com/qouteall) and contributors, licensed under [Apache-2.0](LICENSE).

* [Wiki](https://qouteall.fun/immptl/wiki/)
* [Original project on Modrinth](https://modrinth.com/mod/immersiveportals)
* [Original project on CurseForge](https://www.curseforge.com/minecraft/mc-mods/immersive-portals-mod)
* [Discord server](https://discord.gg/BZxgURK)
