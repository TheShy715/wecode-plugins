#!/usr/bin/env python3
"""Offline repository checks and deterministic distributable archives. No installer."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import unicodedata
import zipfile

SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")
IDENTIFIER = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")
MAX_BYTES = 256 * 1024 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, f"Duplicate JSON key: {key}")
            result[key] = value
        return result
    require(path.stat().st_size <= 8 * 1024 * 1024, "JSON metadata exceeds byte budget")
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite JSON")))


def valid_version(value):
    if not isinstance(value, str) or not SEMVER.fullmatch(value):
        return False
    base = value.split("+", 1)[0]
    for section in (value.split("+", 1)[1:] + base.split("-", 1)[1:]):
        if any(not part for part in section.split(".")):
            return False
    if "-" in base:
        return all(not (part.isdigit() and len(part) > 1 and part.startswith("0"))
                   for part in base.split("-", 1)[1].split("."))
    return True


def local_file(root, relative):
    require(isinstance(relative, str) and relative, "Empty file path")
    path = PurePosixPath(relative)
    require(len(relative.encode("utf-8")) <= 1024 and len(path.parts) <= 32 and
            not any(unicodedata.category(char) == "Cc" for char in relative), "Package path exceeds bounds or contains controls")
    require(not path.is_absolute() and str(path) == relative and
            all(part not in (".", "..") for part in path.parts) and
            "\\" not in relative and ":" not in relative,
            f"Unsafe package path: {relative}")
    target = root
    for part in path.parts:
        target = target / part
        require(not target.is_symlink(), f"Symbolic link is not distributable: {relative}")
    require(target.is_file() and stat.S_ISREG(target.stat().st_mode), f"Missing regular file: {relative}")
    require(target.resolve().is_relative_to(root.resolve()), f"File escaped source: {relative}")
    return target


def verify(root):
    root = root.resolve()
    catalog = read_json(local_file(root, "catalog.json"))
    require(isinstance(catalog, dict) and set(catalog) == {"schema_version", "plugins"} and
            type(catalog["schema_version"]) is int and catalog["schema_version"] == 1,
            "Unsupported catalog structure")
    require(isinstance(catalog["plugins"], list), "plugins must be an array")
    entries, identities = [], set()
    for record in catalog["plugins"]:
        require(isinstance(record, dict) and set(record) in ({
            "id", "version", "path", "manifest_sha256", "validation"}, {
            "id", "version", "path", "manifest_sha256", "validation", "verification"}), "Unexpected catalog record fields")
        ident, version = record["id"], record["version"]
        require(isinstance(ident, str) and IDENTIFIER.fullmatch(ident), "Invalid plugin ID")
        require(valid_version(version), "Invalid version")
        require(ident not in identities, "Duplicate plugin ID")
        identities.add(ident)
        require(record["path"] == f"plugins/{ident}", "Plugin directory must match its ID")
        manifest_path = local_file(root, record["path"] + "/plugin.json")
        require(manifest_path.stat().st_size <= 8 * 1024 * 1024, "Manifest exceeds byte budget")
        manifest_bytes = manifest_path.read_bytes()
        require(record["manifest_sha256"] == sha(manifest_bytes), "Manifest checksum mismatch")
        require(record["validation"] in ("source-only", "host-verified"), "Invalid validation label")
        if record["validation"] == "source-only":
            require("verification" not in record, "Source-only entry cannot claim host verification")
        else:
            receipt_ref = record.get("verification", {})
            require(isinstance(receipt_ref, dict) and set(receipt_ref) == {"path", "sha256"}, "Host verification needs a reviewed evidence receipt")
            require(receipt_ref["path"] == f"validation/{ident}-{version}.json", "Unexpected verification receipt path")
            receipt_path = local_file(root, receipt_ref["path"])
            require(sha(receipt_path.read_bytes()) == receipt_ref["sha256"], "Evidence receipt checksum mismatch")
            receipt = read_json(receipt_path)
            require(isinstance(receipt, dict) and set(receipt) == {"plugin", "version", "host_revision", "platform", "package_digest", "installed_revision", "manifest_sha256", "checks"}, "Unexpected evidence receipt fields")
            require(receipt["manifest_sha256"] == record["manifest_sha256"], "Evidence is not bound to this manifest")
            require(receipt["plugin"] == ident and receipt["version"] == version, "Evidence identity mismatch")
            require(isinstance(receipt["host_revision"], str) and re.fullmatch(r"[0-9a-f]{40}", receipt["host_revision"]), "Expected exact host revision")
            require(isinstance(receipt["platform"], str) and 0 < len(receipt["platform"]) <= 100, "Missing observed platform")
            require(isinstance(receipt["package_digest"], str) and DIGEST.fullmatch(receipt["package_digest"]), "Missing observed Core digest")
            require(type(receipt["installed_revision"]) is int and receipt["installed_revision"] > 0, "Missing installed revision")
            checks = receipt["checks"]
            require(isinstance(checks, dict) and set(checks) == {"install", "describe", "invoke", "process_completion", "upgrade", "disable", "cold_start"}, "Missing lifecycle check statuses")
            require(all(value in ("passed", "failed", "not_run", "not_applicable") for value in checks.values()), "Invalid lifecycle status")
            require(all(checks[key] == "passed" for key in ("install", "describe", "invoke")), "Host verification requires observed install, describe and invoke")
        manifest = read_json(manifest_path)
        require(isinstance(manifest, dict) and type(manifest.get("format")) is int and manifest.get("format") == 1 and manifest.get("identity") == {"id": ident, "version": version},
                "Manifest identity/format mismatch")
        for key in ("protocol", "targets", "entry", "capabilities", "max_concurrency"):
            require(key in manifest, f"Missing manifest field: {key}")
        files = manifest.get("files")
        require(isinstance(files, list) and 0 < len(files) <= 4096, "Invalid file inventory")
        members = {"plugin.json": (manifest_bytes, False)}
        total = len(manifest_bytes)
        for item in files:
            require(isinstance(item, dict) and set(item) <= {"path", "sha256", "executable"} and
                    {"path", "sha256"} <= set(item), "Every package file needs path and sha256")
            name = item["path"]
            require(isinstance(name, str), "File path must be a string")
            require(name.split("/")[0] not in ("plugin.json", ".package-receipt.json"), "Reserved metadata path")
            require(name not in members, "Duplicate file inventory member")
            require(isinstance(item["sha256"], str) and DIGEST.fullmatch(item["sha256"]), "Invalid SHA-256")
            require(isinstance(item.get("executable", False), bool), "Invalid executable flag")
            file_path = local_file(manifest_path.parent, name)
            require(file_path.stat().st_size <= MAX_BYTES, "File exceeds package budget")
            data = file_path.read_bytes()
            require(sha(data) == item["sha256"], f"File checksum mismatch: {name}")
            total += len(data)
            require(total <= MAX_BYTES, "Package exceeds byte budget")
            members[name] = (data, item.get("executable", False))
        entry = manifest["entry"]
        if isinstance(entry, dict) and entry.get("type") == "declarative":
            handlers, capabilities = entry.get("handlers"), manifest["capabilities"]
            require(isinstance(handlers, dict) and isinstance(capabilities, list), "Invalid declarative contract")
            require(all(isinstance(cap, dict) and isinstance(cap.get("name"), str) for cap in capabilities), "Invalid capability")
            names = [cap["name"] for cap in capabilities]
            require(len(names) == len(set(names)) and set(names) == set(handlers), "Handlers must cover exactly the capabilities")
            require(not manifest.get("state") and not manifest.get("ui"), "Declarative packages cannot declare state/UI")
            for cap in capabilities:
                handler = handlers[cap["name"]]
                require(isinstance(handler, dict), "Invalid declarative handler")
                kind = handler.get("type")
                if kind == "resource_read":
                    paths = handler.get("paths")
                    require(cap.get("effect") == "read_only" and isinstance(paths, list) and paths and
                            all(isinstance(path, str) for path in paths) and len(paths) == len(set(paths)) and
                            all(path in members and path != "plugin.json" for path in paths), "Resource handler needs read-only effect and declared files")
                elif kind == "managed_script_start":
                    if record["validation"] == "host-verified":
                        require(checks["process_completion"] == "passed", "Managed script verification requires terminal process and business-output checks")
                    require(cap.get("effect") in ("workspace_write", "external_side_effect"), "Managed scripts require a side-effect declaration")
                    require(isinstance(handler.get("path"), str) and handler.get("path") in members and handler.get("path") != "plugin.json", "Script entry must be declared")
                    runtime = handler.get("runtime")
                    if runtime is not None:
                        require(isinstance(runtime, dict) and runtime.get("id") in ("python3", "node"), "Script runtime must be python3 or node")
                    else:
                        require(members[handler["path"]][1], "Native script entry must be executable")
                else:
                    raise ValueError("Unknown declarative handler")
        entries.append((ident, version, members))
    return entries


def build(root, output):
    entries = verify(root)
    require(entries, "No plugins yet: refusing to create an empty release")
    require(not output.exists() and not output.is_symlink(), "Output must not already exist")
    output.mkdir(parents=False)
    checksums = []
    for ident, version, members in entries:
        filename = f"{ident}-{version}.zip"
        target = output / filename
        with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_STORED) as archive:
            for name, (data, executable) in sorted(members.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | (0o755 if executable else 0o644)) << 16
                archive.writestr(info, data)
        checksums.append(f"{sha(target.read_bytes())}  {filename}\n")
    (output / "SHA256SUMS").write_text("".join(checksums), encoding="utf-8")
    return len(entries)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "build"])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "build":
            require(args.output is not None, "build requires --output")
            count = build(args.root, args.output)
        else:
            require(args.output is None, "--output is only valid for build")
            count = len(verify(args.root))
        print(json.dumps({"operation": args.command, "packages": count, "host_installation_verified": False}))
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.exit(1, f"marketplace: {error}\n")


if __name__ == "__main__":
    main()
