#!/usr/bin/env python3
"""Generate a dependency-free Xcode project from an Xcode Pocket JSON export.

The generated target is always PocketApp. This generator does not compile or sign
anything: run xcodebuild on macOS after it finishes. Nested .swift files and UTF-8 Resources/ files are supported.
Web files outside Resources/ may coexist but are not compiled.
"""

import argparse
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
POCKET_GENERATOR_VERSION = 2
RESOURCE_SUFFIXES = {".json", ".txt", ".csv", ".plist", ".xml", ".md", ".html", ".css", ".js"}


def uid(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24].upper()


def quoted(value):
    # OpenStep plist strings need backslash/quote escapes, not JSON's \u form.
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def settings_block(settings):
    return "{ " + " ".join(f"{key} = {quoted(value)};" for key, value in settings.items()) + " }"


def read_input(path):
    raw = path.read_bytes()
    if len(raw) > MAX_BYTES:
        raise ValueError("Project JSON exceeds 4 MB.")
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("format") != "xcode-pocket-project":
        raise ValueError("Expected an xcode-pocket-project JSON export.")
    files = data.get("files")
    if not isinstance(files, dict) or not 1 <= len(files) <= 200:
        raise ValueError("Project must contain 1–200 text files.")
    if data.get("version") not in (1, 2):
        raise ValueError("Unsupported project schema.")
    paths = set()
    for name, content in files.items():
        if (not isinstance(name, str) or not name or len(name) > 240
                or unicodedata.normalize("NFC", name) != name
                or re.search(r"[\\\x00-\x1f\x7f]", name)
                or any(not part or part != part.strip() or part in (".", "..", "__proto__", "constructor", "prototype") for part in name.split("/"))
                or not isinstance(content, str)):
            raise ValueError("Invalid relative text file path or content.")
        if name.casefold() in paths:
            raise ValueError("Paths differ only by letter case.")
        paths.add(name.casefold())
        if name.startswith("Resources/") and Path(name).suffix.lower() not in RESOURCE_SUFFIXES:
            raise ValueError("Resources supports UTF-8 text data only; binary/assets/localization not supported yet.")
        if name.startswith("Resources/") and "\x00" in content:
            raise ValueError("Binary resource content is not supported.")
    for name in paths:
        parts = name.split("/")
        if any("/".join(parts[:i]) in paths for i in range(1, len(parts))):
            raise ValueError("File/folder path conflict.")
    swift = {name: content for name, content in files.items() if name.endswith(".swift")}
    if not swift:
        raise ValueError("No .swift files found. Native compilation needs Swift source, including an @main app.")
    # macOS commonly uses case-insensitive file systems.
    if len({name.casefold() for name in swift}) != len(swift):
        raise ValueError("Swift filenames differ only by letter case.")
    return data, swift


def build_project(swift, bundle_id, deployment_target, resources=None):
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

    def array(values):
        return "(" + ", ".join(values) + ("," if values else "") + ")"

    obj(ids["product"], f"isa = PBXFileReference; explicitFileType = wrapper.application; includeInIndex = 0; path = {TARGET}.app; sourceTree = BUILT_PRODUCTS_DIR;")
    obj(ids["sources-group"], f"isa = PBXGroup; children = {array(file_ids)}; path = Sources; sourceTree = {quoted('<group>')};")
    obj(ids["products"], f"isa = PBXGroup; children = ({ids['product']},); name = Products; sourceTree = {quoted('<group>')};")
    main_children = [ids["sources-group"], ids["products"]] + ([uid("resources-folder")] if resources else [])
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
    if not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", bundle_id):
        raise ValueError("Bundle ID must use dot-separated letters, numbers or hyphens.")
    if not app_name.strip() or len(app_name) > 80 or any(ord(c) < 32 for c in app_name):
        raise ValueError("Display name must contain 1–80 printable characters.")
    if not re.fullmatch(r"(?:1[7-9]|2[0-9])\.[0-9]{1,2}", deployment_target):
        raise ValueError("Deployment target must be an iOS version from 17.0 to 29.x.")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("Output directory must be empty or absent.")
    project, target_id = build_project(swift, bundle_id, deployment_target, resources)
    sources = output / "Sources"
    scheme_dir = output / (TARGET + ".xcodeproj") / "xcshareddata" / "xcschemes"
    sources.mkdir(parents=True, exist_ok=True)
    scheme_dir.mkdir(parents=True)
    for name, content in swift.items():
        destination = sources / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    for name, content in resources.items():
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
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
    return {"project": str(output / (TARGET + ".xcodeproj")), "scheme": TARGET, "swift_files": len(swift), "bundle_id": bundle_id, "signed": False, "generator_version": POCKET_GENERATOR_VERSION, "resource_files": len(resources)}


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
