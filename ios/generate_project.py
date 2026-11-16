#!/usr/bin/env python3
"""Generate a plain Xcode project for the rbit iOS app.

Run from this directory:  python generate_project.py
The project intentionally has no external dependencies. Re-run this script
after adding new Swift files so they appear in the target.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_DIR = ROOT / "Rbit"
PROJECT_DIR = ROOT / "Rbit.xcodeproj"
TARGET = "Rbit"
BUNDLE_ID = "dev.dreyze.rbit.mobile"


def uid(label: str) -> str:
    return hashlib.md5(label.encode("utf-8")).hexdigest()[:24].upper()


def rel(path: Path) -> str:
    return path.relative_to(APP_DIR).as_posix()


swift_files = sorted(APP_DIR.rglob("*.swift"))

main_group = uid("group:main")
products_group = uid("group:products")
app_group = uid("group:Rbit")
sources_phase = uid("phase:sources")
frameworks_phase = uid("phase:frameworks")
resources_phase = uid("phase:resources")
project_config_list = uid("config:project:list")
target_config_list = uid("config:target:list")
project_id = uid("project")
target_id = uid("target")
product_ref = uid("product")
assets_ref = uid("file:Assets.xcassets")
assets_build = uid("build:Assets.xcassets")
info_ref = uid("file:Info.plist")

project_debug = uid("config:project:debug")
project_release = uid("config:project:release")
target_debug = uid("config:target:debug")
target_release = uid("config:target:release")

build_files: dict[str, str] = {}
file_refs: dict[str, str] = {}
for path in swift_files:
    key = rel(path)
    file_refs[key] = uid(f"file:{key}")
    build_files[key] = uid(f"build:{key}")

subdirs: dict[str, list[str]] = {}
root_files: list[str] = []
for path in swift_files:
    key = rel(path)
    parent = path.parent
    if parent == APP_DIR:
        root_files.append(key)
    else:
        folder = parent.relative_to(APP_DIR).as_posix()
        subdirs.setdefault(folder, []).append(key)

subgroup_ids = {folder: uid(f"group:{folder}") for folder in subdirs}


def obj(file_ref: str, key: str) -> str:
    return (
        f"\t\t{file_ref} /* {Path(key).name} */ = {{isa = PBXFileReference; "
        f"lastKnownFileType = sourcecode.swift; path = {Path(key).name}; sourceTree = \"<group>\"; }};\n"
    )


def build_obj(build_ref: str, file_ref: str, key: str) -> str:
    return (
        f"\t\t{build_ref} /* {Path(key).name} in Sources */ = {{isa = PBXBuildFile; "
        f"fileRef = {file_ref} /* {Path(key).name} */; }};\n"
    )


lines: list[str] = []
add = lines.append

add("// !$*UTF8*$!\n")
add("{\n")
add("\tarchiveVersion = 1;\n")
add("\tclasses = {\n\t};\n")
add("\tobjectVersion = 56;\n")
add("\tobjects = {\n\n")

add("/* Begin PBXBuildFile section */\n")
for key in sorted(build_files):
    add(build_obj(build_files[key], file_refs[key], key))
add(
    f"\t\t{assets_build} /* Assets.xcassets in Resources */ = {{isa = PBXBuildFile; "
    f"fileRef = {assets_ref} /* Assets.xcassets */; }};\n"
)
add("/* End PBXBuildFile section */\n\n")

add("/* Begin PBXFileReference section */\n")
for key in sorted(file_refs):
    add(obj(file_refs[key], key))
add(
    f"\t\t{product_ref} /* Rbit.app */ = {{isa = PBXFileReference; explicitFileType = wrapper.application; "
    f"includeInIndex = 0; path = Rbit.app; sourceTree = BUILT_PRODUCTS_DIR; }};\n"
)
add(
    f"\t\t{assets_ref} /* Assets.xcassets */ = {{isa = PBXFileReference; lastKnownFileType = folder.assetcatalog; "
    f"path = Assets.xcassets; sourceTree = \"<group>\"; }};\n"
)
add(
    f"\t\t{info_ref} /* Info.plist */ = {{isa = PBXFileReference; lastKnownFileType = text.plist.xml; "
    f"path = Info.plist; sourceTree = \"<group>\"; }};\n"
)
add("/* End PBXFileReference section */\n\n")

add("/* Begin PBXFrameworksBuildPhase section */\n")
add(
    f"\t\t{frameworks_phase} /* Frameworks */ = {{\n"
    "\t\t\tisa = PBXFrameworksBuildPhase;\n"
    "\t\t\tbuildActionMask = 2147483647;\n"
    "\t\t\tfiles = (\n\t\t\t);\n"
    "\t\t\trunOnlyForDeploymentPostprocessing = 0;\n"
    "\t\t};\n"
)
add("/* End PBXFrameworksBuildPhase section */\n\n")

add("/* Begin PBXGroup section */\n")
add(
    f"\t\t{main_group} = {{\n"
    "\t\t\tisa = PBXGroup;\n"
    "\t\t\tchildren = (\n"
    f"\t\t\t\t{app_group} /* Rbit */,\n"
    f"\t\t\t\t{products_group} /* Products */,\n"
    "\t\t\t);\n"
    "\t\t\tsourceTree = \"<group>\";\n"
    "\t\t};\n"
)
root_children: list[str] = []
for key in sorted(root_files):
    root_children.append(f"\t\t\t\t{file_refs[key]} /* {Path(key).name} */,\n")
root_children.append(f"\t\t\t\t{assets_ref} /* Assets.xcassets */,\n")
root_children.append(f"\t\t\t\t{info_ref} /* Info.plist */,\n")
for folder in sorted(subdirs):
    root_children.append(f"\t\t\t\t{subgroup_ids[folder]} /* {folder} */,\n")
add(
    f"\t\t{app_group} /* Rbit */ = {{\n"
    "\t\t\tisa = PBXGroup;\n"
    "\t\t\tchildren = (\n"
    + "".join(root_children)
    + "\t\t\t);\n"
    "\t\t\tpath = Rbit;\n"
    "\t\t\tsourceTree = \"<group>\";\n"
    "\t\t};\n"
)
for folder in sorted(subdirs):
    children = "".join(
        f"\t\t\t\t{file_refs[key]} /* {Path(key).name} */,\n" for key in sorted(subdirs[folder])
    )
    add(
        f"\t\t{subgroup_ids[folder]} /* {folder} */ = {{\n"
        "\t\t\tisa = PBXGroup;\n"
        "\t\t\tchildren = (\n"
        + children
        + "\t\t\t);\n"
        f"\t\t\tpath = {folder};\n"
        "\t\t\tsourceTree = \"<group>\";\n"
        "\t\t};\n"
    )
add(
    f"\t\t{products_group} /* Products */ = {{\n"
    "\t\t\tisa = PBXGroup;\n"
    "\t\t\tchildren = (\n"
    f"\t\t\t\t{product_ref} /* Rbit.app */,\n"
    "\t\t\t);\n"
    "\t\t\tname = Products;\n"
    "\t\t\tsourceTree = \"<group>\";\n"
    "\t\t};\n"
)
add("/* End PBXGroup section */\n\n")

add("/* Begin PBXNativeTarget section */\n")
add(
    f"\t\t{target_id} /* Rbit */ = {{\n"
    "\t\t\tisa = PBXNativeTarget;\n"
    "\t\t\tbuildConfigurationList = " + target_config_list + " /* Build configuration list for PBXNativeTarget \"Rbit\" */;\n"
    "\t\t\tbuildPhases = (\n"
    f"\t\t\t\t{sources_phase} /* Sources */,\n"
    f"\t\t\t\t{frameworks_phase} /* Frameworks */,\n"
    f"\t\t\t\t{resources_phase} /* Resources */,\n"
    "\t\t\t);\n"
    "\t\t\tbuildRules = (\n\t\t\t);\n"
    "\t\t\tdependencies = (\n\t\t\t);\n"
    "\t\t\tname = Rbit;\n"
    "\t\t\tproductName = Rbit;\n"
    f"\t\t\tproductReference = {product_ref} /* Rbit.app */;\n"
    "\t\t\tproductType = \"com.apple.product-type.application\";\n"
    "\t\t};\n"
)
add("/* End PBXNativeTarget section */\n\n")

add("/* Begin PBXProject section */\n")
add(
    f"\t\t{project_id} /* Project object */ = {{\n"
    "\t\t\tisa = PBXProject;\n"
    "\t\t\tattributes = {\n"
    "\t\t\t\tBuildIndependentTargetsInParallel = 1;\n"
    "\t\t\t\tLastSwiftUpdateCheck = 1600;\n"
    "\t\t\t\tLastUpgradeCheck = 1600;\n"
    "\t\t\t\tTargetAttributes = {\n"
    f"\t\t\t\t\t{target_id} = {{\n"
    "\t\t\t\t\t\tCreatedOnToolsVersion = 16.0;\n"
    "\t\t\t\t\t};\n"
    "\t\t\t\t};\n"
    "\t\t\t};\n"
    "\t\t\tbuildConfigurationList = " + project_config_list + " /* Build configuration list for PBXProject \"Rbit\" */;\n"
    "\t\t\tcompatibilityVersion = \"Xcode 14.0\";\n"
    "\t\t\tdevelopmentRegion = ru;\n"
    "\t\t\thasScannedForEncodings = 0;\n"
    "\t\t\tknownRegions = (\n"
    "\t\t\t\ten,\n"
    "\t\t\t\tBase,\n"
    "\t\t\t\tru,\n"
    "\t\t\t);\n"
    "\t\t\tmainGroup = " + main_group + ";\n"
    "\t\t\tproductRefGroup = " + products_group + " /* Products */;\n"
    "\t\t\tprojectDirPath = \"\";\n"
    "\t\t\tprojectRoot = \"\";\n"
    "\t\t\ttargets = (\n"
    f"\t\t\t\t{target_id} /* Rbit */,\n"
    "\t\t\t);\n"
    "\t\t};\n"
)
add("/* End PBXProject section */\n\n")

add("/* Begin PBXResourcesBuildPhase section */\n")
add(
    f"\t\t{resources_phase} /* Resources */ = {{\n"
    "\t\t\tisa = PBXResourcesBuildPhase;\n"
    "\t\t\tbuildActionMask = 2147483647;\n"
    "\t\t\tfiles = (\n"
    f"\t\t\t\t{assets_build} /* Assets.xcassets in Resources */,\n"
    "\t\t\t);\n"
    "\t\t\trunOnlyForDeploymentPostprocessing = 0;\n"
    "\t\t};\n"
)
add("/* End PBXResourcesBuildPhase section */\n\n")

add("/* Begin PBXSourcesBuildPhase section */\n")
source_lines = "".join(
    f"\t\t\t\t{build_files[key]} /* {Path(key).name} in Sources */,\n"
    for key in sorted(build_files)
)
add(
    f"\t\t{sources_phase} /* Sources */ = {{\n"
    "\t\t\tisa = PBXSourcesBuildPhase;\n"
    "\t\t\tbuildActionMask = 2147483647;\n"
    "\t\t\tfiles = (\n"
    + source_lines
    + "\t\t\t);\n"
    "\t\t\trunOnlyForDeploymentPostprocessing = 0;\n"
    "\t\t};\n"
)
add("/* End PBXSourcesBuildPhase section */\n\n")

project_debug_settings = """\t\t\t\tALWAYS_SEARCH_USER_PATHS = NO;
\t\t\t\tASSETCATALOG_COMPILER_GENERATE_SWIFT_ASSET_SYMBOL_EXTENSIONS = YES;
\t\t\t\tCLANG_ENABLE_MODULES = YES;
\t\t\t\tCLANG_ENABLE_OBJC_ARC = YES;
\t\t\t\tCOPY_PHASE_STRIP = NO;
\t\t\t\tDEBUG_INFORMATION_FORMAT = dwarf;
\t\t\t\tENABLE_STRICT_OBJC_MSGSEND = YES;
\t\t\t\tENABLE_TESTABILITY = YES;
\t\t\t\tENABLE_USER_SCRIPT_SANDBOXING = YES;
\t\t\t\tGCC_C_LANGUAGE_STANDARD = gnu17;
\t\t\t\tGCC_DYNAMIC_NO_PIC = NO;
\t\t\t\tGCC_NO_COMMON_BLOCKS = YES;
\t\t\t\tGCC_OPTIMIZATION_LEVEL = 0;
\t\t\t\tGCC_PREPROCESSOR_DEFINITIONS = (
\t\t\t\t\t"DEBUG=1",
\t\t\t\t\t"$(inherited)",
\t\t\t\t);
\t\t\t\tIPHONEOS_DEPLOYMENT_TARGET = 17.0;
\t\t\t\tLOCALIZATION_PREFERS_STRING_CATALOGS = YES;
\t\t\t\tMTL_ENABLE_DEBUG_INFO = INCLUDE_SOURCE;
\t\t\t\tMTL_FAST_MATH = YES;
\t\t\t\tONLY_ACTIVE_ARCH = YES;
\t\t\t\tSDKROOT = iphoneos;
\t\t\t\tSWIFT_ACTIVE_COMPILATION_CONDITIONS = "DEBUG $(inherited)";
\t\t\t\tSWIFT_OPTIMIZATION_LEVEL = "-Onone";
\t\t\t\tSWIFT_STRICT_CONCURRENCY = minimal;
"""

project_release_settings = """\t\t\t\tALWAYS_SEARCH_USER_PATHS = NO;
\t\t\t\tASSETCATALOG_COMPILER_GENERATE_SWIFT_ASSET_SYMBOL_EXTENSIONS = YES;
\t\t\t\tCLANG_ENABLE_MODULES = YES;
\t\t\t\tCLANG_ENABLE_OBJC_ARC = YES;
\t\t\t\tCOPY_PHASE_STRIP = NO;
\t\t\t\tDEBUG_INFORMATION_FORMAT = "dwarf-with-dsym";
\t\t\t\tENABLE_NS_ASSERTIONS = NO;
\t\t\t\tENABLE_STRICT_OBJC_MSGSEND = YES;
\t\t\t\tENABLE_USER_SCRIPT_SANDBOXING = YES;
\t\t\t\tGCC_C_LANGUAGE_STANDARD = gnu17;
\t\t\t\tGCC_NO_COMMON_BLOCKS = YES;
\t\t\t\tIPHONEOS_DEPLOYMENT_TARGET = 17.0;
\t\t\t\tLOCALIZATION_PREFERS_STRING_CATALOGS = YES;
\t\t\t\tMTL_ENABLE_DEBUG_INFO = NO;
\t\t\t\tMTL_FAST_MATH = YES;
\t\t\t\tSDKROOT = iphoneos;
\t\t\t\tSWIFT_COMPILATION_MODE = wholemodule;
\t\t\t\tSWIFT_OPTIMIZATION_LEVEL = "-O";
\t\t\t\tSWIFT_STRICT_CONCURRENCY = minimal;
\t\t\t\tVALIDATE_PRODUCT = YES;
"""

target_settings = """\t\t\t\tASSETCATALOG_COMPILER_APPICON_NAME = AppIcon;
\t\t\t\tASSETCATALOG_COMPILER_GLOBAL_ACCENT_COLOR_NAME = AccentColor;
\t\t\t\tCODE_SIGN_STYLE = Automatic;
\t\t\t\tCURRENT_PROJECT_VERSION = 1;
\t\t\t\tDEVELOPMENT_TEAM = "";
\t\t\t\tENABLE_PREVIEWS = YES;
\t\t\t\tGENERATE_INFOPLIST_FILE = NO;
\t\t\t\tINFOPLIST_FILE = Rbit/Info.plist;
\t\t\t\tIPHONEOS_DEPLOYMENT_TARGET = 17.0;
\t\t\t\tLD_RUNPATH_SEARCH_PATHS = (
\t\t\t\t\t"$(inherited)",
\t\t\t\t\t"@executable_path/Frameworks",
\t\t\t\t);
\t\t\t\tMARKETING_VERSION = 1.0;
\t\t\t\tPRODUCT_BUNDLE_IDENTIFIER = %s;
\t\t\t\tPRODUCT_NAME = "$(TARGET_NAME)";
\t\t\t\tSUPPORTED_PLATFORMS = "iphoneos iphonesimulator";
\t\t\t\tSUPPORTS_MACCATALYST = NO;
\t\t\t\tSUPPORTS_MAC_DESIGNED_FOR_IPHONE_IPAD = NO;
\t\t\t\tSWIFT_EMIT_LOC_STRINGS = YES;
\t\t\t\tSWIFT_STRICT_CONCURRENCY = minimal;
\t\t\t\tSWIFT_VERSION = 5.0;
\t\t\t\tTARGETED_DEVICE_FAMILY = 1;
""" % BUNDLE_ID

add("/* Begin XCBuildConfiguration section */\n")
add(
    f"\t\t{project_debug} /* Debug */ = {{\n"
    "\t\t\tisa = XCBuildConfiguration;\n"
    "\t\t\tbuildSettings = {\n"
    + project_debug_settings
    + "\t\t\t};\n"
    "\t\t\tname = Debug;\n"
    "\t\t};\n"
)
add(
    f"\t\t{project_release} /* Release */ = {{\n"
    "\t\t\tisa = XCBuildConfiguration;\n"
    "\t\t\tbuildSettings = {\n"
    + project_release_settings
    + "\t\t\t};\n"
    "\t\t\tname = Release;\n"
    "\t\t};\n"
)
add(
    f"\t\t{target_debug} /* Debug */ = {{\n"
    "\t\t\tisa = XCBuildConfiguration;\n"
    "\t\t\tbuildSettings = {\n"
    + target_settings
    + "\t\t\t};\n"
    "\t\t\tname = Debug;\n"
    "\t\t};\n"
)
add(
    f"\t\t{target_release} /* Release */ = {{\n"
    "\t\t\tisa = XCBuildConfiguration;\n"
    "\t\t\tbuildSettings = {\n"
    + target_settings
    + "\t\t\t};\n"
    "\t\t\tname = Release;\n"
    "\t\t};\n"
)
add("/* End XCBuildConfiguration section */\n\n")

add("/* Begin XCConfigurationList section */\n")
add(
    f"\t\t{project_config_list} /* Build configuration list for PBXProject \"Rbit\" */ = {{\n"
    "\t\t\tisa = XCConfigurationList;\n"
    "\t\t\tbuildConfigurations = (\n"
    f"\t\t\t\t{project_debug} /* Debug */,\n"
    f"\t\t\t\t{project_release} /* Release */,\n"
    "\t\t\t);\n"
    "\t\t\tdefaultConfigurationIsVisible = 0;\n"
    "\t\t\tdefaultConfigurationName = Release;\n"
    "\t\t};\n"
)
add(
    f"\t\t{target_config_list} /* Build configuration list for PBXNativeTarget \"Rbit\" */ = {{\n"
    "\t\t\tisa = XCConfigurationList;\n"
    "\t\t\tbuildConfigurations = (\n"
    f"\t\t\t\t{target_debug} /* Debug */,\n"
    f"\t\t\t\t{target_release} /* Release */,\n"
    "\t\t\t);\n"
    "\t\t\tdefaultConfigurationIsVisible = 0;\n"
    "\t\t\tdefaultConfigurationName = Release;\n"
    "\t\t};\n"
)
add("/* End XCConfigurationList section */\n")

add("\t};\n")
add(f"\trootObject = {project_id} /* Project object */;\n")
add("}\n")

PROJECT_DIR.mkdir(parents=True, exist_ok=True)
(PROJECT_DIR / "project.pbxproj").write_text("".join(lines), encoding="utf-8", newline="\n")

scheme_dir = PROJECT_DIR / "xcshareddata" / "xcschemes"
scheme_dir.mkdir(parents=True, exist_ok=True)
buildable = (
    f'<BuildableReference BuildableIdentifier="primary" BlueprintIdentifier="{target_id}" '
    f'BuildableName="Rbit.app" BlueprintName="Rbit" ReferencedContainer="container:Rbit.xcodeproj"/>'
)
scheme = f'''<?xml version="1.0" encoding="UTF-8"?>
<Scheme LastUpgradeVersion="1600" version="1.7">
   <BuildAction parallelizeBuildables="YES" buildImplicitDependencies="YES">
      <BuildActionEntries>
         <BuildActionEntry buildForTesting="YES" buildForRunning="YES" buildForProfiling="YES" buildForArchiving="YES" buildForAnalyzing="YES">
            {buildable}
         </BuildActionEntry>
      </BuildActionEntries>
   </BuildAction>
   <TestAction buildConfiguration="Debug" selectedDebuggerIdentifier="Xcode.DebuggerFoundation.Debugger.LLDB" selectedLauncherIdentifier="Xcode.DebuggerFoundation.Launcher.LLDB" shouldUseLaunchSchemeArgsEnv="YES" shouldAutocreateTestPlan="YES">
   </TestAction>
   <LaunchAction buildConfiguration="Debug" selectedDebuggerIdentifier="Xcode.DebuggerFoundation.Debugger.LLDB" selectedLauncherIdentifier="Xcode.DebuggerFoundation.Launcher.LLDB" launchStyle="0" useCustomWorkingDirectory="NO" ignoresPersistentStateOnLaunch="NO" debugDocumentVersioning="YES" debugServiceExtension="internal" allowLocationSimulation="YES">
      <BuildableProductRunnable runnableDebuggingMode="0">
         {buildable}
      </BuildableProductRunnable>
   </LaunchAction>
   <ProfileAction buildConfiguration="Release" shouldUseLaunchSchemeArgsEnv="YES" savedToolIdentifier="" useCustomWorkingDirectory="NO" debugDocumentVersioning="YES">
      <BuildableProductRunnable runnableDebuggingMode="0">
         {buildable}
      </BuildableProductRunnable>
   </ProfileAction>
   <AnalyzeAction buildConfiguration="Debug"/>
   <ArchiveAction buildConfiguration="Release" revealArchiveInOrganizer="YES"/>
</Scheme>
'''
(scheme_dir / "Rbit.xcscheme").write_text(scheme, encoding="utf-8", newline="\n")

print(f"generated {PROJECT_DIR / 'project.pbxproj'} with {len(swift_files)} Swift files")
