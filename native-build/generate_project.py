#!/usr/bin/env python3
"""Generate a dependency-free Xcode project from an Xcode Pocket JSON export.

The generated target is always PocketApp. This generator does not compile or sign
anything: run xcodebuild on macOS after it finishes. Nested .swift files and UTF-8 Resources/ files are supported.
Schema v3 additionally supports validated base64 image Resources.
Web files outside Resources/ may coexist but are not compiled.
"""

import argparse
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET

MAX_BYTES = 4_000_000
TARGET = "PocketApp"
POCKET_GENERATOR_VERSION = 4
RESOURCE_SUFFIXES = {".json", ".txt", ".csv", ".plist", ".xml", ".md", ".html", ".css", ".js"}


def uid(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24].upper()


def quoted(value):
    # OpenStep plist strings need backslash/quote escapes, not JSON's \u form.
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def settings_block(settings):
    return "{ " + " ".join(f"{key} = {quoted(value)};" for key, value in settings.items()) + " }"


def validate_binary(name, entry):
    if not isinstance(entry, dict) or set(entry) != {"mime", "base64"}:
        raise ValueError("Binary entries require exactly mime and base64.")
    mime, encoded = entry["mime"], entry["base64"]
    extensions = {"image/png": {".png"}, "image/jpeg": {".jpg", ".jpeg"},
                  "image/gif": {".gif"}, "image/webp": {".webp"}}
    if (not isinstance(mime, str) or mime not in extensions
            or not name.startswith("Resources/")
            or Path(name).suffix.lower() not in extensions[mime]):
        raise ValueError("Binary image MIME, suffix and Resources/ destination must agree.")
    if not isinstance(encoded, str) or not encoded or len(encoded) > ((MAX_BYTES + 2) // 3) * 4:
        raise ValueError("Invalid or oversized base64 resource.")
    try:
        blob = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Invalid base64 resource.") from exc
    if base64.b64encode(blob).decode("ascii") != encoded:
        raise ValueError("Resource base64 must be canonical without whitespace.")
    signature = {
        "image/png": len(blob) >= 24 and blob[:8] == b"\x89PNG\r\n\x1a\n" and blob[12:16] == b"IHDR",
        "image/jpeg": len(blob) >= 4 and blob[:3] == b"\xff\xd8\xff" and blob[-2:] == b"\xff\xd9",
        "image/gif": len(blob) >= 13 and blob[:6] in (b"GIF87a", b"GIF89a"),
        "image/webp": len(blob) >= 16 and blob[:4] == b"RIFF" and blob[8:12] == b"WEBP",
    }
    if not signature[mime]:
        raise ValueError("Image bytes do not match the declared format or header is truncated.")
    return blob


CATALOG = "Resources/Assets.xcassets"


def validate_catalog(files, binaries):
    """Bounded universal PNG/JPEG image sets, not arbitrary asset catalogs."""
    all_paths = set(files) | set(binaries)
    asset_paths = {p for p in all_paths if any(part.lower().endswith(".xcassets") for part in p.split("/"))}
    if not asset_paths:
        return set()
    if any(not p.startswith(CATALOG + "/") for p in asset_paths):
        raise ValueError("Only Resources/Assets.xcassets is supported.")

    def document(path):
        if path not in files:
            raise ValueError("Asset catalog Contents.json is missing: " + path)
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate asset JSON key.")
                result[key] = value
            return result
        value = json.loads(files[path], object_pairs_hook=unique)
        if not isinstance(value, dict):
            raise ValueError("Asset Contents.json must be an object.")
        info = value.get("info")
        if (not isinstance(info, dict) or set(info) - {"version", "author"}
                or type(info.get("version")) is not int or info["version"] != 1
                or ("author" in info and not isinstance(info["author"], str))):
            raise ValueError("Asset Contents.json requires info.version 1.")
        return value

    root = CATALOG + "/Contents.json"
    if set(document(root)) != {"info"}:
        raise ValueError("Unsupported catalog root metadata.")
    accepted, sets = {root}, set()
    for path in asset_paths - {root}:
        relative = path[len(CATALOG) + 1:].split("/")
        if (len(relative) != 2 or not relative[0].endswith(".imageset")
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,79}", relative[0][:-9])):
            raise ValueError("Only named universal image sets are supported; no app icons, colors or nested asset groups.")
        sets.add(relative[0])
    for name in sorted(sets):
        folder = CATALOG + "/" + name + "/"
        metadata = folder + "Contents.json"
        value = document(metadata)
        if set(value) != {"info", "images"} or not isinstance(value["images"], list) or not 1 <= len(value["images"]) <= 3:
            raise ValueError("Image sets require 1–3 universal scaled image entries.")
        accepted.add(metadata)
        scales, used = set(), set()
        for entry in value["images"]:
            if (not isinstance(entry, dict) or set(entry) != {"idiom", "scale", "filename"}
                    or entry["idiom"] != "universal" or entry["scale"] not in ("1x", "2x", "3x")
                    or not isinstance(entry["filename"], str)
                    or "/" in entry["filename"] or "\\" in entry["filename"]
                    or entry["filename"] in ("", ".", "..")):
                raise ValueError("Unsupported asset image entry or unsafe image reference.")
            path = folder + entry["filename"]
            if entry["scale"] in scales or path in used:
                raise ValueError("Duplicate image scale or reference.")
            if path not in binaries or binaries[path]["mime"] not in ("image/png", "image/jpeg"):
                raise ValueError("Image sets must reference existing PNG/JPEG binary resources.")
            scales.add(entry["scale"])
            used.add(path)
            accepted.add(path)
    if asset_paths != accepted:
        raise ValueError("Asset catalog contains unreferenced or unsupported files.")
    return accepted


def read_input(path):
    # Base64 overhead counts toward the same serialized browser/export limit.
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("Project JSON exceeds 4 MB encoded limit.")
    raw = path.read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("format") != "xcode-pocket-project":
        raise ValueError("Expected an xcode-pocket-project JSON export.")
    if data.get("version") not in (1, 2, 3):
        raise ValueError("Unsupported project schema.")
    files, binaries = data.get("files"), data.get("binaries", {})
    if not isinstance(files, dict) or not isinstance(binaries, dict) or not 1 <= len(files) + len(binaries) <= 200:
        raise ValueError("Project must contain 1–200 total files.")
    if data["version"] != 3 and "binaries" in data:
        raise ValueError("Binary resources require schema v3.")
    paths, total = set(), 0
    for name, content in list(files.items()) + list(binaries.items()):
        if (not isinstance(name, str) or not name or len(name) > 240
                or unicodedata.normalize("NFC", name) != name
                or re.search(r"[\\\x00-\x1f\x7f]", name)
                or any(not part or part != part.strip() or part in (".", "..", "__proto__", "constructor", "prototype") for part in name.split("/"))):
            raise ValueError("Invalid relative file path.")
        if name.casefold() in paths:
            raise ValueError("Duplicate paths or paths differ only by letter case.")
        paths.add(name.casefold())
        if name in files:
            if not isinstance(content, str):
                raise ValueError("Text file content must be a string.")
            if name.startswith("Resources/") and Path(name).suffix.lower() not in RESOURCE_SUFFIXES:
                raise ValueError("Text Resources supports UTF-8 data; images belong in the v3 binaries map.")
            if name.startswith("Resources/") and "\x00" in content:
                raise ValueError("Binary resource content is not supported as text.")
            total += len(content.encode("utf-8"))
        else:
            total += len(validate_binary(name, content))
        if total > MAX_BYTES:
            raise ValueError("Decoded project content exceeds 4 MB.")
    for name in paths:
        parts = name.split("/")
        if any("/".join(parts[:i]) in paths for i in range(1, len(parts))):
            raise ValueError("File/folder path conflict.")
    validate_catalog(files, binaries)
    swift = {name: content for name, content in files.items() if name.endswith(".swift")}
    if not swift:
        raise ValueError("No .swift files found. Native compilation needs Swift source, including an @main app.")
    return data, swift


def build_project(swift, bundle_id, deployment_target, resources=None, catalog=False):
    resources = resources or {}
    ids = {key: uid(key) for key in (
        "project", "main", "sources-group", "products", "product", "target",
        "sources-phase", "frameworks-phase", "resources-phase", "project-configs",
        "target-configs", "project-debug", "project-release", "target-debug", "target-release",
    )}
    objects = []

    def obj(identifier, body):
        objects.append(f"\t\t{identifier} = {{ {body} }};")

    file_ids = []
    build_ids = []
    resource_ids = []
    for name in sorted(swift):
        file_id, build_id = uid("file:" + name), uid("build:" + name)
        file_ids.append(file_id)
        build_ids.append(build_id)
        obj(file_id, f"isa = PBXFileReference; lastKnownFileType = sourcecode.swift; path = {quoted(name)}; sourceTree = {quoted('<group>')};")
        obj(build_id, f"isa = PBXBuildFile; fileRef = {file_id};")

    if resources:
        rid, bid = uid("resources-folder"), uid("resources-build")
        obj(rid, f'isa = PBXFileReference; lastKnownFileType = folder; path = Resources; sourceTree = "<group>";')
        obj(bid, f"isa = PBXBuildFile; fileRef = {rid};")
        resource_ids.append(bid)

    if catalog:
        rid, bid = uid("asset-catalog"), uid("asset-catalog-build")
        obj(rid, 'isa = PBXFileReference; lastKnownFileType = wrapper.assetcatalog; path = Assets.xcassets; sourceTree = "<group>";')
        obj(bid, f"isa = PBXBuildFile; fileRef = {rid};")
        resource_ids.append(bid)

    def array(values):
        return "(" + ", ".join(values) + ("," if values else "") + ")"

    obj(ids["product"], f"isa = PBXFileReference; explicitFileType = wrapper.application; includeInIndex = 0; path = {TARGET}.app; sourceTree = BUILT_PRODUCTS_DIR;")
    obj(ids["sources-group"], f"isa = PBXGroup; children = {array(file_ids)}; path = Sources; sourceTree = {quoted('<group>')};")
    obj(ids["products"], f"isa = PBXGroup; children = ({ids['product']},); name = Products; sourceTree = {quoted('<group>')};")
    main_children = [ids["sources-group"], ids["products"]] + ([uid("resources-folder")] if resources else []) + ([uid("asset-catalog")] if catalog else [])
    obj(ids["main"], f"isa = PBXGroup; children = {array(main_children)}; sourceTree = {quoted('<group>')};")
    for phase, isa, members in (("sources-phase", "PBXSourcesBuildPhase", build_ids), ("frameworks-phase", "PBXFrameworksBuildPhase", []), ("resources-phase", "PBXResourcesBuildPhase", resource_ids)):
        obj(ids[phase], f"isa = {isa}; buildActionMask = 2147483647; files = {array(members)}; runOnlyForDeploymentPostprocessing = 0;")

    common_project = {"CLANG_ENABLE_MODULES": "YES", "SDKROOT": "iphoneos", "IPHONEOS_DEPLOYMENT_TARGET": deployment_target}
    common_target = {
        "PRODUCT_NAME": TARGET,
        "PRODUCT_BUNDLE_IDENTIFIER": bundle_id,
        "INFOPLIST_FILE": "Info.plist",
        "GENERATE_INFOPLIST_FILE": "NO",
        "SWIFT_VERSION": "5.0",
        "TARGETED_DEVICE_FAMILY": "1",
        "SUPPORTED_PLATFORMS": "iphoneos iphonesimulator",
        "SUPPORTS_MACCATALYST": "NO",
        "CODE_SIGN_STYLE": "Automatic",
        "CURRENT_PROJECT_VERSION": "1",
        "MARKETING_VERSION": "1.0",
        "LD_RUNPATH_SEARCH_PATHS": "$(inherited) @executable_path/Frameworks",
        "ENABLE_USER_SCRIPT_SANDBOXING": "YES",
    }
    for config in ("Debug", "Release"):
        lower = config.lower()
        project_settings = dict(common_project)
        target_settings = dict(common_target)
        target_settings["SWIFT_OPTIMIZATION_LEVEL"] = "-Onone" if config == "Debug" else "-O"
        if config == "Debug":
            target_settings["SWIFT_ACTIVE_COMPILATION_CONDITIONS"] = "DEBUG"
        obj(ids["project-" + lower], f"isa = XCBuildConfiguration; buildSettings = {settings_block(project_settings)}; name = {config};")
        obj(ids["target-" + lower], f"isa = XCBuildConfiguration; buildSettings = {settings_block(target_settings)}; name = {config};")
    for owner in ("project", "target"):
        obj(ids[owner + "-configs"], f"isa = XCConfigurationList; buildConfigurations = ({ids[owner + '-debug']}, {ids[owner + '-release']},); defaultConfigurationIsVisible = 0; defaultConfigurationName = Release;")
    obj(ids["target"], f"isa = PBXNativeTarget; buildConfigurationList = {ids['target-configs']}; buildPhases = ({ids['sources-phase']}, {ids['frameworks-phase']}, {ids['resources-phase']},); buildRules = (); dependencies = (); name = {TARGET}; productName = {TARGET}; productReference = {ids['product']}; productType = {quoted('com.apple.product-type.application')};")
    obj(ids["project"], f"isa = PBXProject; attributes = {{ LastUpgradeCheck = 1600; }}; buildConfigurationList = {ids['project-configs']}; compatibilityVersion = {quoted('Xcode 14.0')}; developmentRegion = en; hasScannedForEncodings = 0; knownRegions = (en, Base,); mainGroup = {ids['main']}; productRefGroup = {ids['products']}; projectDirPath = {quoted('')}; projectRoot = {quoted('')}; targets = ({ids['target']},);")
    text = "// !$*UTF8*$!\n{\n\tarchiveVersion = 1;\n\tclasses = {};\n\tobjectVersion = 56;\n\tobjects = {\n" + "\n".join(objects) + f"\n\t}};\n\trootObject = {ids['project']};\n}}\n"
    return text, ids["target"]


def write_scheme(path, target_id):
    root = ET.Element("Scheme", LastUpgradeVersion="1600", version="1.3")
    action = ET.SubElement(root, "BuildAction", parallelizeBuildables="YES", buildImplicitDependencies="YES")
    entries = ET.SubElement(action, "BuildActionEntries")
    entry = ET.SubElement(entries, "BuildActionEntry", buildForTesting="YES", buildForRunning="YES", buildForProfiling="YES", buildForArchiving="YES", buildForAnalyzing="YES")

    def reference(parent):
        ET.SubElement(parent, "BuildableReference", BuildableIdentifier="primary", BlueprintIdentifier=target_id, BuildableName=TARGET + ".app", BlueprintName=TARGET, ReferencedContainer="container:" + TARGET + ".xcodeproj")

    reference(entry)
    launch = ET.SubElement(root, "LaunchAction", buildConfiguration="Debug", selectedDebuggerIdentifier="Xcode.DebuggerFoundation.Debugger.LLDB", selectedLauncherIdentifier="Xcode.IDEFoundation.Launcher.LLDB", launchStyle="0", useCustomWorkingDirectory="NO", ignoresPersistentStateOnLaunch="NO", debugDocumentVersioning="YES", debugServiceExtension="internal", allowLocationSimulation="YES")
    reference(ET.SubElement(launch, "BuildableProductRunnable", runnableDebuggingMode="0"))
    ET.SubElement(root, "AnalyzeAction", buildConfiguration="Debug")
    ET.SubElement(root, "ArchiveAction", buildConfiguration="Release", revealArchiveInOrganizer="YES")
    ET.indent(root)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def generate(input_path, output, bundle_id, app_name, deployment_target):
    data, swift = read_input(input_path)
    resources = {name: content for name, content in data["files"].items() if name.startswith("Resources/")}
    binaries = {name: validate_binary(name, entry) for name, entry in data.get("binaries", {}).items()}
    catalog_paths = validate_catalog(data["files"], data.get("binaries", {}))
    raw_resources = {name: content for name, content in {**resources, **binaries}.items() if name not in catalog_paths}
    if not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", bundle_id):
        raise ValueError("Bundle ID must use dot-separated letters, numbers or hyphens.")
    if not app_name.strip() or len(app_name) > 80 or any(ord(c) < 32 for c in app_name):
        raise ValueError("Display name must contain 1–80 printable characters.")
    if not re.fullmatch(r"(?:1[7-9]|2[0-9])\.[0-9]{1,2}", deployment_target):
        raise ValueError("Deployment target must be an iOS version from 17.0 to 29.x.")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("Output directory must be empty or absent.")
    project, target_id = build_project(swift, bundle_id, deployment_target, raw_resources, bool(catalog_paths))
    sources = output / "Sources"
    scheme_dir = output / (TARGET + ".xcodeproj") / "xcshareddata" / "xcschemes"
    sources.mkdir(parents=True, exist_ok=True)
    scheme_dir.mkdir(parents=True)
    for name, content in swift.items():
        destination = sources / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    for name, content in resources.items():
        destination = output / (name[len("Resources/"):] if name in catalog_paths else name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    for name, content in binaries.items():
        destination = output / (name[len("Resources/"):] if name in catalog_paths else name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    (output / (TARGET + ".xcodeproj") / "project.pbxproj").write_text(project, encoding="utf-8")
    write_scheme(scheme_dir / (TARGET + ".xcscheme"), target_id)
    info = {
        "CFBundleDevelopmentRegion": "en", "CFBundleDisplayName": app_name,
        "CFBundleExecutable": "$(EXECUTABLE_NAME)", "CFBundleIdentifier": "$(PRODUCT_BUNDLE_IDENTIFIER)",
        "CFBundleInfoDictionaryVersion": "6.0", "CFBundleName": "$(PRODUCT_NAME)", "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "$(MARKETING_VERSION)", "CFBundleVersion": "$(CURRENT_PROJECT_VERSION)",
        "LSRequiresIPhoneOS": True, "UILaunchScreen": {},
        "UIApplicationSceneManifest": {"UIApplicationSupportsMultipleScenes": False},
        "UISupportedInterfaceOrientations": ["UIInterfaceOrientationPortrait", "UIInterfaceOrientationLandscapeLeft", "UIInterfaceOrientationLandscapeRight"],
    }
    with (output / "Info.plist").open("wb") as stream:
        plistlib.dump(info, stream)
    return {"project": str(output / (TARGET + ".xcodeproj")), "scheme": TARGET, "swift_files": len(swift), "bundle_id": bundle_id, "signed": False, "generator_version": POCKET_GENERATOR_VERSION, "resource_files": len(resources) + len(binaries), "binary_resource_files": len(binaries), "asset_catalogs": int(bool(catalog_paths))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bundle-id", default=os.environ.get("IOS_BUNDLE_ID", "cz.davidvolf.pocketapp"))
    parser.add_argument("--app-name", default=os.environ.get("IOS_APP_NAME", "Pocket App"))
    parser.add_argument("--deployment-target", default=os.environ.get("IOS_DEPLOYMENT_TARGET", "17.0"))
    args = parser.parse_args()
    try:
        result = generate(args.input, args.output, args.bundle_id, args.app_name, args.deployment_target)
    except (ValueError, OSError) as exc:
        print(f"Project generation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
